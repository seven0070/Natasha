/* Vision: images, screenshots, OCR, UI interpretation, camera, video, accessibility tree. */

import { api } from "../api.js";
import { el, clear, toast } from "../ui.js";
import { section, stat, json, markdownBlock } from "../components.js";
import { register } from "../router.js";

function dataUrlFromFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error(`could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

function showResult(host, payload) {
  clear(host);
  if (!payload || !payload.ok) {
    host.append(el("div", { class: "empty error" },
      (payload && (payload.error || payload.detail)) || "no result"));
    if (payload) host.append(json(payload, { label: "raw response" }));
    return;
  }
  const suspicious = payload.suspicious
    ? el("div", { class: "small error" },
      "The content contained instructions aimed at the agent. It was treated as data only.")
    : null;
  host.append(el("div", { class: "row tight", style: "margin-bottom:.4rem" },
    el("span", { class: "chip chip--ok" }, `model ${payload.model || "ocr"}`),
    payload.provider ? el("span", { class: "chip" }, payload.provider) : null,
    payload.width ? el("span", { class: "chip" }, `${payload.width}x${payload.height}`) : null,
    el("span", { class: "chip" }, "untrusted external content")),
    suspicious,
    markdownBlock(payload.text || payload.ocr_text || "(nothing)"),
    json(payload, { label: "raw analysis" }));
}

export async function renderVision(container) {
  const caps = await api.get("/api/vision/capabilities");
  const prompt = el("textarea", { placeholder: "what do you want to know about the image? (optional)" });
  const path = el("input", { placeholder: "path to an image on this machine (inside the allowed roots)" });
  const file = el("input", { type: "file", accept: "image/*" });
  const result = el("div", {});
  const lastFrame = { value: "" };

  async function analyse(payload) {
    clear(result).append(el("div", { class: "spinner" }));
    try {
      const response = await api.post("/api/vision/analyse", { prompt: prompt.value, ...payload });
      showResult(result, response);
      if (response.stored_path && response.width) previewFrame(response.stored_path);
    } catch (error) { clear(result).append(el("div", { class: "empty error" }, error.message)); }
  }

  async function previewFrame(storedPath) {
    const host = container.querySelector("[data-frame]");
    if (!host) return;
    clear(host);
    try {
      const response = await api.request("GET", "/api/artifacts/download",
        { query: { path: storedPath } , raw: true });
      const blob = await response.blob();
      lastFrame.value = URL.createObjectURL(blob);
      host.append(el("img", { src: lastFrame.value, alt: "analysed frame",
                              style: "max-width:100%;border-radius:8px;border:1px solid var(--border)" }));
    } catch (error) {
      host.append(el("div", { class: "small muted" }, `frame not previewable: ${error.message}`));
    }
  }

  container.append(el("div", { class: "grid", style: "margin-bottom:.8rem" },
    stat("vision model", caps.available ? "ready" : "unavailable", (caps.models || []).join(", ")),
    stat("local OCR", caps.ocr ? "available" : "not installed", "pytesseract"),
    stat("camera", caps.camera ? "available" : "not detected", "opencv"),
    stat("accessibility tree", caps.accessibility_tree ? "available" : "platform limited",
      (caps.accessibility || {}).detail || ""),
    stat("video frames", caps.video ? "ffmpeg found" : "needs ffmpeg", "frame sampling"))),
    el("div", { class: "split" },
      el("div", {},
        section("Look at something",
          el("label", {}, "Prompt"), prompt,
          el("label", {}, "Image on disk"), path,
          el("div", { class: "row tight", style: "margin-top:.4rem" },
            el("button", { class: "btn btn--primary", onclick: () => {
              if (!path.value.trim()) { toast("A path is required", "error"); return; }
              analyse({ path: path.value.trim(), prompt: prompt.value });
            } }, "Analyse path"),
            el("button", { class: "btn", onclick: async () => {
              try {
                const payload = await api.post("/api/vision/ocr", { path: path.value.trim() });
                showResult(result, payload.ok ? { ...payload, text: payload.text, model: "ocr" } : payload);
              } catch (error) { toast(error.message, "error"); }
            } }, "OCR the file"),
            el("button", { class: "btn", onclick: async () => {
              try { showResult(result, await api.post("/api/vision/screen", { prompt: prompt.value, path: "", data_url: "" })); }
              catch (error) { toast(error.message, "error"); }
            } }, "Screenshot the screen"),
            el("button", { class: "btn", onclick: async () => {
              try {
                const payload = await api.post("/api/vision/camera", { index: 0, prompt: prompt.value });
                showResult(result, payload);
                if (payload.stored_path) previewFrame(payload.stored_path);
              } catch (error) { toast(error.message, "error"); }
            } }, "Camera frame"),
            el("button", { class: "btn", onclick: async () => {
              try { showResult(result, await api.post("/api/vision/documents", { path: path.value.trim() })); }
              catch (error) { toast(error.message, "error"); }
            } }, "Read the document")),
          el("label", {}, "Upload an image", ),
          file,
          el("button", {
            class: "btn", style: "margin-top:.4rem", onclick: async () => {
              const chosen = file.files && file.files[0];
              if (!chosen) { toast("Choose an image first", "error"); return; }
              try {
                const dataUrl = await dataUrlFromFile(chosen);
                const response = await api.post("/api/vision/analyse", { data_url: dataUrl, prompt: prompt.value });
                showResult(result, response);
                if (response.stored_path) previewFrame(response.stored_path);
              } catch (error) { toast(error.message, "error"); }
            },
          }, "Analyse upload"),
          el("label", {}, "UI understanding (screenshot + accessibility tree)", ),
          el("button", {
            class: "btn", onclick: async () => {
              try {
                const payload = await api.post("/api/vision/ui", { path: path.value.trim(), prompt: prompt.value });
                showResult(result, payload);
              } catch (error) { toast(error.message, "error"); }
            },
          }, "Interpret the current UI")),
        section("Accessibility tree", el("div", { "data-a11y": "1" }, el("span", { class: "spinner" })))),
      el("div", {},
        section("Analysis", result),
        section("Frame", el("div", { "data-frame": "1" }, el("span", { class: "muted small" }, "nothing captured yet")))));

  api.get("/api/vision/accessibility").then((tree) => {
    const host = container.querySelector("[data-a11y]");
    clear(host);
    if (!tree.ok) {
      host.append(el("div", { class: "small muted" }, tree.error || "unavailable on this platform"));
      return;
    }
    host.append(el("div", { class: "small muted" },
      `${tree.count} elements via ${tree.backend} on ${tree.platform}`),
      el("pre", {}, (tree.nodes || []).slice(0, 60)
        .map((node) => `${node.role}: ${node.name}${node.bounds ? ` @${node.bounds.join(",")}` : ""}`).join("\n")));
  }).catch((error) => clear(container.querySelector("[data-a11y]")).append(el("div", { class: "small error" }, error.message)));
}

register({
  id: "vision", title: "Vision", icon: "👁",
  subtitle: "images, screenshots, OCR, UI, camera, video",
  render: renderVision,
});
