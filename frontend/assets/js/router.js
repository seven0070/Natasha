/* Hash router. Routes are registered by the view modules; the nav is built from the registry. */

const routes = new Map();
let current = null;
let onNavigate = null;

export function register(definition) {
  routes.set(definition.id, definition);
  return definition;
}

export function routeList() {
  return Array.from(routes.values());
}

export function currentRoute() {
  return current;
}

export function parse(value) {
  const raw = String(value || "").replace(/^#\/?/, "");
  const [path, queryString] = raw.split("?");
  const [id, arg] = path.split("/");
  const query = {};
  for (const [key, value2] of new URLSearchParams(queryString || "")) query[key] = value2;
  return { id: id || "", arg: arg || "", query };
}

export function href(id, arg = "", query = {}) {
  const params = new URLSearchParams(query);
  const search = params.toString();
  return `#/${id}${arg ? `/${arg}` : ""}${search ? `?${search}` : ""}`;
}

export function go(id, arg = "", query = {}) {
  const target = href(id, arg, query);
  if (location.hash === target) render();
  else location.hash = target;
}

export async function render() {
  const { id, arg, query } = parse(location.hash);
  const definition = routes.get(id) || routes.get("chat") || routeList()[0];
  if (!definition) return;
  const container = document.getElementById("view");
  const title = document.getElementById("view-title");
  if (!container) return;
  current = { definition, arg, query };
  if (title) {
    title.innerHTML = "";
    title.append(document.createTextNode(definition.title || ""));
    if (definition.subtitle) {
      const small = document.createElement("div");
      small.className = "small muted";
      small.textContent = definition.subtitle;
      title.append(small);
    }
  }
  container.innerHTML = "";
  const nav = document.querySelectorAll("[data-route]");
  nav.forEach((node) => node.classList.toggle("active", node.dataset.route === definition.id));
  try {
    await definition.render(container, { arg, query });
  } catch (error) {
    container.innerHTML = "";
    const block = document.createElement("div");
    block.className = "empty error";
    block.textContent = `Could not open this screen: ${error.message || error}`;
    container.append(block);
  }
  if (onNavigate) onNavigate(definition);
}

export function start(navigateCallback) {
  onNavigate = navigateCallback || null;
  window.addEventListener("hashchange", () => { render(); });
  if (!location.hash) location.hash = "#/chat";
  else render();
}
