/* Chat: streaming replies, history, attachments, tool and mission activity, approvals, artifacts.
   Obsidian Intelligence Stitch Design Implementation with real Natasha backend. */

import { api } from "../api.js";
import { store } from "./../store.js";
import { el, clear, fmtTime, toast, modal, titleCase } from "./../ui.js";
import { section, toolCallCard, approvalCard, json, stat, markdownBlock, emptyState } from "./../components.js";
import { go, register } from "./../router.js";

const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;
const state = {
  conversationId: "",
  missionId: "",
  attachments: [],
  busy: false,
  streaming: null,
  webSearch: false,
  codeMode: false,
};

function messageNode(role, content, { timestamp, model, attachments = [] } = {}) {
  const isOwner = role === "owner" || role === "user";
  const timeStr = timestamp ? fmtTime(timestamp) : "just now";
  const ownerName = store.ownerId || "Owner";

  if (isOwner) {
    const attachmentNodes = attachments.map((item) =>
      el("div", { class: "attach", style: "margin-top:0.35rem" },
        el("span", { class: "material-symbols-outlined icon-xs" }, item.kind === "image" ? "image" : "draft"),
        el("span", { class: "mono small" }, item.name)));

    return el("div", { class: "msg msg--owner" },
      el("div", { class: "msg__avatar" },
        el("span", { class: "material-symbols-outlined icon-sm" }, "person")),
      el("div", { class: "msg__body" },
        el("div", { class: "msg__header" },
          el("span", { class: "msg__name" }, ownerName),
          el("span", { class: "msg__time" }, timeStr)),
        el("div", { style: "white-space:pre-wrap;line-height:1.6" }, content),
        attachmentNodes.length ? el("div", { class: "row tight", style: "margin-top:0.4rem" }, ...attachmentNodes) : null));
  }

  // Assistant turn
  const node = el("div", { class: "msg msg--assistant" },
    el("div", { class: "msg__avatar" },
      el("img", { src: "/assets/logo.svg", alt: "Natasha", width: "20", height: "20" })),
    el("div", { class: "msg__body" },
      el("div", { class: "msg__header" },
        el("span", { class: "msg__name" }, "Natasha"),
        el("span", { class: "msg__badge" }, model ? `model: ${model}` : "natasha-4.5-ultra"),
        el("span", { class: "msg__time" }, timeStr)),
      el("div", { class: "markdown" }, markdownBlock(content)),
      createActionToolbar(content)));

  return node;
}

function createActionToolbar(content) {
  const bar = el("div", { class: "action-toolbar" });
  const leftGroup = el("div", { class: "action-group" },
    el("button", {
      class: "action-btn", title: "Copy response text",
      onclick: () => {
        navigator.clipboard.writeText(content);
        toast("Response copied to clipboard");
      },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "content_copy")),
    el("button", {
      class: "action-btn", title: "Helpful response",
      onclick: (e) => {
        e.currentTarget.style.color = "var(--ok)";
        toast("Feedback recorded: Helpful");
      },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "thumb_up")),
    el("button", {
      class: "action-btn", title: "Unhelpful response",
      onclick: (e) => {
        e.currentTarget.style.color = "var(--warn)";
        toast("Feedback recorded: Unhelpful");
      },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "thumb_down")),
    el("button", {
      class: "action-btn", title: "Read aloud",
      onclick: async () => {
        try {
          await api.post("/api/voice/speak", { text: content.slice(0, 500) });
          toast("Speaking response audio…");
        } catch {
          if ("speechSynthesis" in window) {
            const utter = new SpeechSynthesisUtterance(content.slice(0, 500));
            window.speechSynthesis.speak(utter);
          }
        }
      },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "volume_up")));

  const wordCount = (content.trim().split(/\s+/).filter(Boolean).length) || 0;
  const tokenEst = Math.round(wordCount * 1.3);
  const rightGroup = el("div", { class: "action-group" },
    el("span", { class: "small muted mono" }, `~${tokenEst} tokens`));

  bar.append(leftGroup, rightGroup);
  return bar;
}

function conversationKey() { return state.conversationId || "new"; }

function persist() {
  try {
    localStorage.setItem(`natasha.chat.${conversationKey()}`, JSON.stringify({
      missionId: state.missionId,
      attachments: state.attachments.map((item) => ({ name: item.name, kind: item.kind, text: item.text })),
    }));
  } catch { /* history is a convenience */ }
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
    row.append(el("span", { class: "attach" },
      el("span", { class: "material-symbols-outlined icon-sm" }, item.kind === "image" ? "image" : "description"),
      `${item.name}`,
      el("button", {
        class: "icon-btn small", title: "Remove attachment", style: "width:18px;height:18px;margin-left:4px",
        onclick: () => {
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
    host.append(el("div", { class: "row tight", style: "margin-top:0.75rem" },
      el("a", { class: "btn btn--primary", target: "_blank",
                href: `/api/artifacts/download?path=${encodeURIComponent(path)}` }, "Download"),
      el("button", { class: "btn", onclick: () => host.remove() }, "Close")));
  }).catch((error) => toast(error.message, "error"));
}

function streamReply(container, message, attachments, scroll) {
  const node = el("div", { class: "msg msg--assistant" },
    el("div", { class: "msg__avatar" },
      el("img", { src: "/assets/logo.svg", alt: "Natasha", width: "20", height: "20" })),
    el("div", { class: "msg__body" }));
  const body = node.querySelector(".msg__body");

  // Header info
  body.append(el("div", { class: "msg__header" },
    el("span", { class: "msg__name" }, "Natasha"),
    el("span", { class: "msg__badge" }, "streaming synthesis…"),
    el("span", { class: "msg__time" }, "just now")));

  // Live execution timeline
  const timeline = el("div", { class: "execution-timeline" },
    el("div", { class: "execution-timeline__head" },
      el("span", { class: "row tight" },
        el("span", { class: "pulse-dot" }),
        el("span", {}, "Execution Trace")),
      el("span", { class: "mono small" }, "stream active")),
    el("div", { class: "execution-timeline__steps" },
      el("div", { class: "execution-step complete" },
        el("span", { class: "material-symbols-outlined icon-sm step-icon" }, "done_all"),
        el("span", {}, "Context received")),
      el("div", { class: "execution-step complete" },
        el("span", { class: "material-symbols-outlined icon-sm step-icon" }, "done_all"),
        el("span", {}, "Memory search"))));

  const textContent = el("div", { class: "markdown" }, el("span", { class: "spinner" }));
  const activity = el("div", { style: "display:flex;flex-direction:column;gap:.4rem;margin-top:.5rem" });

  body.append(timeline, textContent);
  scroll.append(node);
  scroll.scrollTop = scroll.scrollHeight;

  let text = "";
  const startTime = Date.now();

  const handle = api.chatStream({
    message: state.codeMode ? `[CODE MODE ACTIVE]\n${message}` : message,
    conversationId: state.conversationId,
    missionId: state.missionId,
    images: attachments.filter((item) => item.kind === "image").map((item) => item.text),
    documents: attachments.filter((item) => item.kind !== "image")
      .map((item) => ({ text: item.text, source: item.name, kind: item.kind })),
    onEvent: (event) => {
      if (event.type === "token") {
        text += event.text || "";
        clear(textContent).append(markdownBlock(text));
      } else if (event.type === "stream_unavailable") {
        clear(textContent).append(el("div", { class: "small muted" }, "Waiting for complete turn response…"));
      } else if (event.type === "tool") {
        activity.append(toolCallCard(event, { onOpenArtifact: openArtifact }));
      } else if (event.type === "round") {
        activity.append(el("div", { class: "small muted" }, `Execution round ${event.round}`));
      } else if (event.type === "error") {
        activity.append(el("div", { class: "small error" }, event.error || "The execution round failed"));
      } else if (event.type === "turn_finished") {
        const result = event.result || {};
        const replyText = result.reply || text || "(no reply)";
        clear(textContent).append(markdownBlock(replyText));

        // Update header
        const header = body.querySelector(".msg__header");
        if (header) {
          clear(header).append(
            el("span", { class: "msg__name" }, "Natasha"),
            el("span", { class: "msg__badge" }, `model: ${result.model || "natasha-4.5-ultra"}`),
            el("span", { class: "msg__time" }, `${Math.round(result.duration_ms || (Date.now() - startTime))} ms`));
        }

        // Complete timeline
        const steps = timeline.querySelector(".execution-timeline__steps");
        if (steps) {
          steps.append(
            el("div", { class: "execution-step complete" },
              el("span", { class: "material-symbols-outlined icon-sm step-icon" }, "verified"),
              el("span", {}, "Completed")));
        }

        if (result.offline_placeholder) {
          body.append(el("div", { class: "small error", style: "margin-top:0.4rem" },
            "No cloud model provider configured: reply generated by local offline fallback."));
        }

        const requested = result.approvals_requested || [];
        requested.forEach((request) => {
          activity.append(request && request.id
            ? approvalCard(request, { onDecided: () => store.refreshStatus() })
            : el("div", { class: "approval-card" }, el("strong", {}, "Approval required"), json(request)));
        });
        if (requested.length) store.refreshStatus();

        if (result.mission_id) {
          activity.append(el("button", {
            class: "btn small btn--primary", style: "margin-top:0.4rem",
            onclick: () => go("missions", result.mission_id),
          }, `View task ${String(result.mission_id).slice(0, 8)}`));
        }

        if (activity.childNodes.length) body.append(activity);
        body.append(createActionToolbar(replyText));

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
  scroll.append(messageNode("owner", trimmed, { attachments: state.attachments }));
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
      scroll.append(messageNode(message.role, message.content, {
        timestamp: message.created_at,
        model: message.model,
      }));
    });
    scroll.scrollTop = scroll.scrollHeight;
  } catch { /* new conversation */ }
}

export async function renderChat(container, { arg, query } = {}) {
  if (arg) state.conversationId = arg;
  if (query && query.mission) state.missionId = query.mission;
  restore();

  const scroll = el("div", { class: "chat__scroll" });

  // Floating Composer Box & Dock
  const textarea = el("textarea", {
    class: "composer-textarea",
    placeholder: "Ask Natasha anything or press / for prompts... (Shift+Enter for new line)",
    rows: "1",
    onkeydown: (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        const value = textarea.value;
        textarea.value = "";
        textarea.style.height = "auto";
        send(value, container);
      }
    },
    oninput: () => {
      textarea.style.height = "auto";
      textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
    },
  });

  const fileInput = el("input", {
    type: "file", multiple: true, style: "display:none",
    onchange: async (event) => {
      await attach(event.target.files);
      renderControls(container);
      event.target.value = "";
    },
  });

  // Insert Capabilities Popover Menu
  const popoverMenu = el("div", { class: "popover-menu", hidden: true },
    el("div", { class: "small muted", style: "padding:0.25rem 0.5rem;font-weight:600" }, "CAPABILITIES"),
    el("button", {
      class: "popover-menu__item", type: "button",
      onclick: () => { popoverMenu.hidden = true; fileInput.click(); },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "upload_file"), "Upload Files / Code"),
    el("button", {
      class: "popover-menu__item", type: "button",
      onclick: () => { popoverMenu.hidden = true; go("vision"); },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "photo_camera"), "Capture Camera / Vision"),
    el("button", {
      class: "popover-menu__item", type: "button",
      onclick: () => { popoverMenu.hidden = true; go("computer"); },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "screenshot_monitor"), "Inspect Desktop / Screen"),
    el("button", {
      class: "popover-menu__item", type: "button",
      onclick: () => {
        popoverMenu.hidden = true;
        state.webSearch = !state.webSearch;
        webSearchPill.classList.toggle("active", state.webSearch);
        toast(`Web Deep Research ${state.webSearch ? "enabled" : "disabled"}`);
      },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "travel_explore"), "Toggle Web Research"),
    el("button", {
      class: "popover-menu__item", type: "button",
      onclick: () => {
        popoverMenu.hidden = true;
        state.codeMode = !state.codeMode;
        codePill.classList.toggle("active", state.codeMode);
        toast(`Code Mode ${state.codeMode ? "enabled" : "disabled"}`);
      },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "terminal"), "Toggle Code Mode"));

  const attachTrigger = el("button", {
    class: "icon-btn", type: "button", title: "Attach multi-modal context",
    onclick: (e) => {
      e.stopPropagation();
      popoverMenu.hidden = !popoverMenu.hidden;
    },
  }, el("span", { class: "material-symbols-outlined" }, "add_circle"));

  document.addEventListener("click", (e) => {
    if (!popoverMenu.hidden && !popoverMenu.contains(e.target) && e.target !== attachTrigger) {
      popoverMenu.hidden = true;
    }
  });

  const webSearchPill = el("button", {
    class: "pill-btn", type: "button",
    onclick: () => {
      state.webSearch = !state.webSearch;
      webSearchPill.classList.toggle("active", state.webSearch);
    },
  }, el("span", { class: "material-symbols-outlined icon-sm" }, "public"), "Search Web");

  const codePill = el("button", {
    class: "pill-btn", type: "button",
    onclick: () => {
      state.codeMode = !state.codeMode;
      codePill.classList.toggle("active", state.codeMode);
    },
  }, el("span", { class: "material-symbols-outlined icon-sm" }, "code_blocks"), "Code Mode");

  const micBtn = el("button", {
    class: "icon-btn", type: "button", title: "Switch to Voice Arena",
    onclick: () => go("voice"),
  }, el("span", { class: "material-symbols-outlined" }, "mic"));

  const sendBtn = el("button", {
    class: "btn-send", type: "button", title: "Send message",
    onclick: () => {
      const val = textarea.value;
      textarea.value = "";
      textarea.style.height = "auto";
      send(val, container);
    },
  }, el("span", { class: "material-symbols-outlined icon-sm" }, "arrow_upward"));

  // Suggestion Micro-Chips
  const suggestionRow = el("div", { class: "suggestion-chips" },
    el("button", {
      class: "suggestion-chip", type: "button",
      onclick: () => { textarea.value = "Inspect latency bottleneck across local and cloud models"; textarea.focus(); },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "tune"), "Model latency benchmarks"),
    el("button", {
      class: "suggestion-chip", type: "button",
      onclick: () => { textarea.value = "List active tasks and summarize current mission progress"; textarea.focus(); },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "checklist"), "Summarize active tasks"),
    el("button", {
      class: "suggestion-chip", type: "button",
      onclick: () => { textarea.value = "Review recent workspace artifacts and files"; textarea.focus(); },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "folder_open"), "Review workspace artifacts"),
    el("button", {
      class: "suggestion-chip", type: "button",
      onclick: () => { textarea.value = "Deploy an autonomous coding agent to refactor module"; textarea.focus(); },
    }, el("span", { class: "material-symbols-outlined icon-sm" }, "smart_toy"), "Delegate to agent fleet"));

  // Assemble Composer Box
  const composerBox = el("div", { class: "composer-box" },
    textarea,
    el("div", { "data-controls": "1", style: "margin:0.25rem 0" }),
    el("div", { class: "composer-bar" },
      el("div", { class: "composer-bar__left", style: "position:relative" },
        attachTrigger,
        popoverMenu,
        fileInput,
        webSearchPill,
        codePill),
      el("div", { class: "composer-bar__right" },
        micBtn,
        sendBtn)));

  const composerDock = el("div", { class: "composer-dock-container" },
    el("div", { class: "composer-dock" },
      suggestionRow,
      composerBox));

  container.append(
    el("div", { class: "chat" },
      scroll,
      composerDock));

  renderControls(container);

  if (store.pendingApprovals) {
    scroll.append(el("div", { class: "approval-card" },
      el("strong", {}, `${store.pendingApprovals} approval(s) waiting`),
      el("div", { class: "small muted" }, "Sensitive actions require your explicit authorization."),
      el("button", { class: "btn small btn--primary", style: "margin-top:0.4rem", onclick: () => go("approvals") }, "Review Approvals")));
  }

  await history(container);
  textarea.focus();
}

/* ---------- conversations transcript view ---------- */
export async function renderConversations(container) {
  const list = el("div", { class: "list" });
  const detail = el("div", {});
  container.append(el("div", { class: "split" }, el("div", {}, section("Conversations", list)), detail));
  const payload = await api.get("/api/conversations");
  const conversations = payload.conversations || [];
  if (!conversations.length) list.append(emptyState("No conversations yet. Start a discussion in Chat."));
  conversations.forEach((conversation) => {
    list.append(el("div", {
      class: "list__item",
      onclick: async () => {
        Array.from(list.children).forEach((node) => node.classList.remove("active"));
        const payload2 = await api.get(`/api/conversations/${conversation.id}`);
        clear(detail).append(section("Transcript",
          ...(payload2.messages || []).map((message) => messageNode(message.role, message.content, {
            timestamp: message.created_at,
            model: message.model,
          }))));
      },
    }, el("div", { class: "bold" }, conversation.id),
      el("div", { class: "small muted" }, `${conversation.messages} messages - ${fmtTime(conversation.updated_at)}`)));
  });
  if (conversations.length) {
    clear(detail).append(section("Transcript", el("p", { class: "muted" }, "Select a conversation to view transcript.")));
  }
}

register({
  id: "chat", title: "Chat", icon: "chat_bubble", subtitle: "Autonomous Conversational Atelier",
  render: renderChat,
});
register({
  id: "conversations", title: "Conversations", icon: "history", subtitle: "Stored transcripts and dialogue history",
  render: renderConversations,
});

export { messageNode, titleCase, stat };
