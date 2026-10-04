/* Chat: streaming replies, history, attachments, tool and mission activity, approvals, artifacts. */

import { api } from "../api.js";
import { store } from "./../store.js";
import { el, clear, fmtTime, toast, markdown, modal, titleCase } from "./../ui.js";
import { section, toolCallCard, approvalCard, json, stat, markdownBlock, emptyState } from "./../components.js";
import { go, register } from "./../router.js";

const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;
const state = { conversationId: "", missionId: "", attachments: [], busy: false, streaming: null };

function messageNode(role, content) {
  return el("div", { class: `msg msg--${role}` },
    el("div", { class: "msg__avatar" }, role === "owner" ? "you" : "N"),
    el("div", { class: "msg__body" }, role === "owner" ? el("div", { style: "white-space:pre-wrap" }, content) : markdownBlock(content)));
}

function conversationKey() { return state.conversationId || "new"; }

function persist() {
  try {
    localStorage.setItem(`natasha.chat.${conversationKey()}`, JSON.stringify({
      missionId: state.missionId,
      attachments: state.attachments.map((item) => ({ name: item.name, kind: item.kind, text: item.text })),
    }));
  } catch { /* history is a convenience, never a requirement */ }
}

function restore() {
  try {
    const saved = JSON.parse(localStorage.getItem(`natasha.chat.${conversationKey()}`) || "{}");
    state.missionId = saved.missionId || "";
    state.attachments = (saved.attachments || []).map((item) => ({ ...item, text: item.text || "" }));
  } catch { state.attachments = []; }
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error(`could not read ${file.name}`));
    reader.readAsText(file);
  });
}

function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error(`could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

async function attach(files) {
  for (const file of files) {
    if (file.size > MAX_ATTACHMENT_BYTES) { toast(`${file.name} is larger than 8 MB`, "error"); continue; }
    const kind = file.type.startsWith("image/") ? "image"
      : file.type.startsWith("audio/") ? "audio"
        : file.type.startsWith("video/") ? "video" : "document";
    try {
      if (kind === "image") state.attachments.push({ name: file.name, kind, text: await readDataUrl(file) });
      else if (kind === "document" || file.type.startsWith("text/")) {
        state.attachments.push({ name: file.name, kind, text: (await readFile(file)).slice(0, 200_000) });
      } else {
        state.attachments.push({ name: file.name, kind, text: `[${kind} attached: ${file.name}]` });
      }
    } catch (error) { toast(error.message, "error"); }
  }
  persist();
}

function attachmentChips(onChange) {
  const row = el("div", { class: "composer__tools" });
  state.attachments.forEach((item, index) => {
    row.append(el("span", { class: "attach" }, `${item.kind}: ${item.name}`,
      el("button", {
        class: "icon-btn small", title: "Remove", onclick: () => {
          state.attachments.splice(index, 1); persist(); onChange();
        },
      }, "✕")));
  });
  return row;
}

function openArtifact(artifact) {
  const path = artifact.path || "";
  modal({
    title: artifact.name || String(path).split("/").pop(),
    body: el("div", {}, el("div", { class: "small muted mono" }, path)),
  });
  api.get("/api/artifacts/content", { path }).then((payload) => {
    const host = document.getElementById("modal-root").querySelector(".modal__box");
    if (!host) return;
    host.append(payload.previewable
      ? el("pre", {}, payload.text || "")
      : el("p", { class: "small muted" }, `Binary artifact (${payload.mime}) - use Download.`));
    host.append(el("div", { class: "row tight" },
      el("a", { class: "btn", target: "_blank",
                href: `/api/artifacts/download?path=${encodeURIComponent(path)}` }, "Download"),
      el("button", { class: "btn", onclick: () => host.remove() }, "Close")));
  }).catch((error) => toast(error.message, "error"));
}

function streamReply(container, message, attachments, scroll) {
  const node = el("div", { class: "msg msg--assistant" },
    el("div", { class: "msg__avatar" }, "N"), el("div", { class: "msg__body" }));
  const body = node.querySelector(".msg__body");
  const activity = el("div", { style: "display:flex;flex-direction:column;gap:.4rem;margin-top:.5rem" });
  let text = "";
  body.append(el("span", { class: "spinner" }));
  scroll.append(node);
  scroll.scrollTop = scroll.scrollHeight;
  const handle = api.chatStream({
    message,
    conversationId: state.conversationId,
    missionId: state.missionId,
    images: attachments.filter((item) => item.kind === "image").map((item) => item.text),
    documents: attachments.filter((item) => item.kind !== "image")
      .map((item) => ({ text: item.text, source: item.name, kind: item.kind })),
    onEvent: (event) => {
      if (event.type === "token") {
        text += event.text || "";
        clear(body).append(markdownBlock(text));
      } else if (event.type === "stream_unavailable") {
        clear(body).append(el("div", { class: "small muted" }, "provider cannot stream; waiting for the full answer…"));
      } else if (event.type === "tool") {
        activity.append(toolCallCard(event, { onOpenArtifact: openArtifact }));
      } else if (event.type === "round") {
        activity.append(el("div", { class: "small muted" }, `round ${event.round}`));
      } else if (event.type === "error") {
        activity.append(el("div", { class: "small error" }, event.error || "the turn failed"));
      } else if (event.type === "turn_finished") {
        const result = event.result || {};
        clear(body).append(markdownBlock(result.reply || text || "(no reply)"));
        body.append(el("div", { class: "msg__meta" },
          `${result.provider || "offline"} / ${result.model || "echo"} - ${Math.round(result.duration_ms || 0)} ms`));
        if (result.offline_placeholder) {
          body.append(el("div", { class: "small error" },
            "No model provider is configured: this reply came from the offline placeholder."));
        }
        const requested = result.approvals_requested || [];
        requested.forEach((request) => {
          activity.append(request && request.id
            ? approvalCard(request, { onDecided: () => store.refreshStatus() })
            : el("div", { class: "approval-card" }, el("strong", {}, "Approval required"), json(request)));
        });
        if (requested.length) store.refreshStatus();
        if (result.mission_id) {
          activity.append(el("button", { class: "btn small", onclick: () => go("missions", result.mission_id) },
            `open mission ${String(result.mission_id).slice(0, 8)}`));
        }
        if (activity.childNodes.length) body.append(activity);
        state.busy = false;
        state.streaming = null;
        state.conversationId = result.conversation_id || state.conversationId;
        renderControls(container);
        store.refreshStatus();
      }
    },
  });
  handle.start();
  state.streaming = handle;
  scroll.scrollTop = scroll.scrollHeight;
}

function send(message, container) {
  const trimmed = String(message || "").trim();
  if (!trimmed || state.busy) return;
  state.busy = true;
  const scroll = container.querySelector(".chat__scroll");
  scroll.append(messageNode("owner", trimmed));
  scroll.scrollTop = scroll.scrollHeight;
  const attachments = state.attachments.slice();
  state.attachments = [];
  persist();
  streamReply(container, trimmed, attachments, scroll);
}

function renderControls(container) {
  const host = container.querySelector("[data-controls]");
  if (!host) return;
  clear(host);
  host.append(attachmentChips(() => renderControls(container)));
}

async function history(container) {
  const scroll = container.querySelector(".chat__scroll");
  if (!state.conversationId) return;
  try {
    const payload = await api.get(`/api/conversations/${state.conversationId}`);
    clear(scroll);
    (payload.messages || []).forEach((message) => {
      scroll.append(messageNode(message.role === "user" ? "owner" : "assistant", message.content));
    });
    scroll.scrollTop = scroll.scrollHeight;
  } catch { /* a new conversation has no history yet */ }
}

export async function renderChat(container, { arg, query } = {}) {
  if (arg) state.conversationId = arg;
  if (query && query.mission) state.missionId = query.mission;
  restore();
  const scroll = el("div", { class: "chat__scroll" });
  const textarea = el("textarea", {
    placeholder: "Ask Natasha…  (Enter to send, Shift+Enter for a new line)",
    onkeydown: (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        const value = textarea.value;
        textarea.value = "";
        send(value, container);
      }
    },
  });
  const fileInput = el("input", {
    type: "file", multiple: true, style: "display:none",
    onchange: async (event) => { await attach(event.target.files); renderControls(container); event.target.value = ""; },
  });
  const missionInput = el("input", { value: state.missionId, placeholder: "mission id (optional)",
    onchange: () => { state.missionId = missionInput.value.trim(); persist(); } });
  const composer = el("div", { class: "composer" },
    el("div", { class: "composer__row" },
      textarea,
      el("button", { class: "btn btn--primary", onclick: () => { const value = textarea.value; textarea.value = ""; send(value, container); } }, "Send")),
    el("div", { class: "composer__tools" },
      el("button", { class: "btn small", onclick: () => fileInput.click() }, "Attach"),
      fileInput,
      el("span", { class: "small muted" }, "images, audio, video, documents"),
      el("span", { style: "width:12rem;display:inline-block" }, missionInput),
      el("button", {
        class: "btn small", onclick: async () => {
          if (!state.conversationId) return;
          if (!window.confirm("Clear this conversation from the screen? The log keeps everything.")) return;
          state.conversationId = ""; state.attachments = []; clear(scroll); renderControls(container);
        },
      }, "New conversation"),
      state.missionId ? el("span", { class: "chip" }, `mission ${state.missionId.slice(0, 8)}`) : null),
    el("div", { "data-controls": "1", class: "composer__tools" }));
  container.append(el("div", { class: "chat" }, scroll, composer));
  renderControls(container);
  if (store.pendingApprovals) {
    scroll.append(el("div", { class: "approval-card" },
      el("strong", {}, `${store.pendingApprovals} approval(s) are waiting`),
      el("div", { class: "small muted" }, "Dangerous actions are queued and cannot run until you decide."),
      el("button", { class: "btn small", style: "margin-top:.4rem", onclick: () => go("approvals") }, "Review approvals")));
  }
  await history(container);
  textarea.focus();
}

/* ---------- conversations ---------- */
export async function renderConversations(container) {
  const list = el("div", { class: "list" });
  const detail = el("div", {});
  container.append(el("div", { class: "split" }, el("div", {}, section("Conversations", list)), detail));
  const payload = await api.get("/api/conversations");
  const conversations = payload.conversations || [];
  if (!conversations.length) list.append(emptyState("No conversations yet. Say something in Chat."));
  conversations.forEach((conversation) => {
    list.append(el("div", {
      class: "list__item",
      onclick: async () => {
        Array.from(list.children).forEach((node) => node.classList.remove("active"));
        const payload2 = await api.get(`/api/conversations/${conversation.id}`);
        clear(detail).append(section("Transcript",
          ...(payload2.messages || []).map((message) => messageNode(message.role === "user" ? "owner" : "assistant", message.content))));
      },
    }, el("div", {}, conversation.id),
      el("div", { class: "small muted" }, `${conversation.messages} messages - ${fmtTime(conversation.updated_at)}`)));
  });
  if (conversations.length) {
    clear(detail).append(section("Transcript", el("p", { class: "muted" }, "Pick a conversation on the left.")));
  }
}

register({
  id: "chat", title: "Chat", icon: "💬", subtitle: "streaming replies, tools, approvals, artifacts",
  render: renderChat,
});
register({
  id: "conversations", title: "Conversations", icon: "🗂", subtitle: "what was said, when",
  render: renderConversations,
});

export { messageNode, titleCase, stat };
