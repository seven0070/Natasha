/* API client: one place that knows about the token, errors and streaming.
   Everything is a relative URL so the same UI works behind the packaged desktop shell, the
   browser preview proxy and plain `natasha serve`. */

const TOKEN_KEY = "natasha.token";

export function resolveApiUrl(path) {
  if (!path || path.startsWith("http://") || path.startsWith("https://") || path.startsWith("ws://") || path.startsWith("wss://")) {
    return path;
  }
  const isTauri = Boolean(window.__TAURI_INTERNALS__ || window.__TAURI__);
  const isLocalHost = location.hostname === "localhost" || location.hostname === "127.0.0.1";
  const defaultHost = window.__NATASHA_BACKEND_URL__ || "http://127.0.0.1:8000";

  if (isTauri && !isLocalHost) {
    const base = defaultHost.replace(/\/+$/, "");
    return `${base}${path.startsWith("/") ? "" : "/"}${path}`;
  }
  return path;
}

export const api = {
  token: localStorage.getItem(TOKEN_KEY) || "",

  setToken(token) {
    this.token = token || "";
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  },

  headers(extra = {}) {
    const headers = { "Content-Type": "application/json", ...extra };
    if (this.token) headers["X-Natasha-Token"] = this.token;
    return headers;
  },

  async request(method, path, { body, query, raw = false } = {}) {
    let url = resolveApiUrl(path);
    if (query) {
      const params = new URLSearchParams();
      for (const [key, value] of Object.entries(query)) {
        if (value !== undefined && value !== null && value !== "") params.set(key, value);
      }
      const qs = params.toString();
      if (qs) url += (url.includes("?") ? "&" : "?") + qs;
    }
    const response = await fetch(url, {
      method,
      headers: this.headers(),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 401 || response.status === 403) {
      const error = new Error("owner authentication required");
      error.status = response.status;
      throw error;
    }
    if (raw) return response;
    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = { detail: text }; }
    }
    if (!response.ok) {
      const detail = payload && (payload.detail || payload.error);
      const error = new Error(typeof detail === "string" ? detail : `request failed (${response.status})`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  },

  get(path, query) { return this.request("GET", path, { query }); },
  post(path, body, query) { return this.request("POST", path, { body, query }); },
  patch(path, body) { return this.request("PATCH", path, { body }); },
  del(path, query) { return this.request("DELETE", path, { query }); },

  /** Server-sent events for a chat turn. Returns an async iterator of parsed events. */
  async *stream(path, body) {
    const response = await fetch(resolveApiUrl(path), { method: "POST", headers: this.headers(), body: JSON.stringify(body) });
    if (!response.ok || !response.body) throw new Error(`stream failed (${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        const line = part.split("\n").find((row) => row.startsWith("data: "));
        if (!line) continue;
        try { yield JSON.parse(line.slice(6)); } catch { /* ignore malformed frame */ }
      }
    }
  },

  /** Websocket chat with an automatic fallback to SSE when the socket cannot be opened. */
  chatStream({ message, conversationId = "", missionId = "", images = [], documents = [], onEvent }) {
    const isTauri = Boolean(window.__TAURI_INTERNALS__ || window.__TAURI__);
    const isLocalHost = location.hostname === "localhost" || location.hostname === "127.0.0.1";
    const defaultHost = window.__NATASHA_BACKEND_URL__ || "http://127.0.0.1:8000";
    let wsHost = location.host;
    let scheme = location.protocol === "https:" ? "wss" : "ws";

    if (isTauri && !isLocalHost) {
      try {
        const parsed = new URL(defaultHost);
        wsHost = parsed.host;
        scheme = parsed.protocol === "https:" ? "wss" : "ws";
      } catch {
        wsHost = "127.0.0.1:8000";
      }
    }
    let socket = null;
    let closed = false;
    const url = `${scheme}://${wsHost}/api/ws/chat`;

    const fallback = async () => {
      try {
        for await (const event of api.stream("/api/chat/stream", {
          message, conversation_id: conversationId, mission_id: missionId, images, documents,
        })) {
          if (closed) break;
          onEvent(event);
        }
      } catch (error) {
        onEvent({ type: "error", error: String(error.message || error) });
      }
    };

    try {
      socket = new WebSocket(url);
      socket.onopen = () => socket.send(JSON.stringify({ type: "auth", token: api.token }));
      socket.onmessage = (event) => {
        try { onEvent(JSON.parse(event.data)); } catch { /* ignore */ }
      };
      socket.onerror = () => { if (!closed) { closed = true; try { socket.close(); } catch {} fallback(); } };
    } catch {
      fallback();
    }
    return {
      send(extra) { if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(extra)); },
      close() { closed = true; try { socket && socket.close(); } catch {} },
      start() {
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({
            message, conversation_id: conversationId, mission_id: missionId, images, documents,
          }));
        } else {
          // The socket is still connecting (or failed): send once it opens.
          const timer = setInterval(() => {
            if (!socket || socket.readyState === WebSocket.OPEN) {
              clearInterval(timer);
              socket && socket.send(JSON.stringify({
                message, conversation_id: conversationId, mission_id: missionId, images, documents,
              }));
            }
          }, 30);
        }
      },
    };
  },
};
