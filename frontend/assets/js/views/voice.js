/* Voice: the microphone -> STT -> Natasha -> TTS -> speaker loop, with barge-in. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast, kv } from "../ui.js";
import { section, table, stat, json } from "../components.js";
import { register } from "../router.js";

export async function renderVoice(container) {
  const capsHost = el("div", {});
  const turnsHost = el("div", {});
  const textInput = el("textarea", { placeholder: "Type what you would say out loud, or press Listen." });
  const speakToggle = el("input", { type: "checkbox", checked: "checked" });
  const seconds = el("input", { type: "number", value: "5", min: "1", max: "60" });
  const language = el("input", { placeholder: "language (en)", value: "en" });
  const status = el("div", { class: "small muted" }, "idle");
  let recorder = null;
  let chunks = [];

  async function refresh() {
    const payload = await api.get("/api/voice/loop");
    const caps = payload.capabilities || {};
    clear(capsHost).append(el("div", { class: "grid" },
      stat("text to speech", caps.text_to_speech ? "ready" : "unavailable",
        (caps.voice || {}).reason || ""),
      stat("speech to text", caps.speech_to_text ? "ready" : "unavailable",
        Object.keys((caps.hearing || {})).filter((key) => (caps.hearing || {})[key]).join(", ")),
      stat("microphone", caps.microphone ? "available" : "not detected", "host input device"),
      stat("barge-in", caps.barge_in ? "supported" : "text only", "interrupt while speaking")));
    clear(turnsHost).append(table([
      { label: "when", value: (row) => fmtTime(row.started_at) },
      { label: "heard", value: (row) => row.transcript },
      { label: "replied", value: (row) => String(row.reply || "").slice(0, 120) },
      { label: "speech", value: (row) => row.audio_path ? `${row.speech_backend} (${row.audio_path.split("/").pop()})` : (row.stages?.tts?.error || "text only") },
      { label: "took", value: (row) => `${Math.round(row.latency_ms)} ms` },
    ], (payload.turns || []).reverse(), { empty: "No voice turns yet." }));
  }

  async function turn(payload) {
    status.textContent = "listening…";
    try {
      const result = await api.post("/api/voice/turn", payload);
      status.textContent = result.ok ? `done in ${Math.round(result.latency_ms)} ms`
        : `did not complete: ${result.error || "unknown reason"}`;
      if (!result.ok && result.error) toast(result.error, "error");
      if (result.audio_path) {
        // Play the reply in the browser: the server host may have no speaker at all.
        try { await new Audio(`/api/artifacts/download?path=${encodeURIComponent(result.audio_path)}`).play(); }
        catch { /* autoplay may be blocked; the file is still listed below */ }
      }
      refresh();
    } catch (error) { status.textContent = "failed"; toast(error.message, "error"); }
  }

  async function startRecording() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast("This browser cannot capture the microphone; use Listen (host microphone) instead.", "error");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recorder = new MediaRecorder(stream);
      chunks = [];
      recorder.ondataavailable = (event) => chunks.push(event.data);
      recorder.onstop = async () => {
        stream.getTracks().forEach((track) => track.stop());
        const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
        const form = new FormData();
        form.append("file", blob, "microphone.webm");
        status.textContent = "transcribing…";
        try {
          const response = await fetch(`/api/voice/turn/audio?language=${encodeURIComponent(language.value || "en")}` +
            `&speak=${speakToggle.checked}`, {
            method: "POST", headers: api.token ? { "X-Natasha-Token": api.token } : {}, body: form,
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || "voice turn failed");
          status.textContent = result.ok ? "done" : `did not complete: ${result.error}`;
          if (!result.ok) toast(result.error || "nothing was heard", "error");
          refresh();
        } catch (error) { status.textContent = "failed"; toast(error.message, "error"); }
      };
      recorder.start();
      status.textContent = "recording… press Stop to send";
    } catch (error) {
      toast(`microphone permission refused: ${error.message}`, "error");
    }
  }

  container.append(el("div", { class: "grid", style: "margin-bottom:.8rem" },
    section("Speak with Natasha",
      el("label", {}, "What to say"), textInput,
      el("div", { class: "row" },
        el("div", { style: "flex:0 0 8rem" }, el("label", {}, "seconds (host mic)"), seconds),
        el("div", { style: "flex:0 0 8rem" }, el("label", {}, "language"), language),
        el("label", { style: "display:flex;gap:.4rem;align-items:center" }, speakToggle, "speak the reply")),
      el("div", { class: "row tight", style: "margin-top:.5rem" },
        el("button", {
          class: "btn btn--primary", onclick: () => turn({
            text: textInput.value, speak: speakToggle.checked, language: language.value,
          }),
        }, "Send text through the voice loop"),
        el("button", { class: "btn", onclick: () => turn({
          text: "", speak: speakToggle.checked, language: language.value,
          seconds: Number(seconds.value || 5),
        }) }, "Listen (host microphone)"),
        el("button", { class: "btn", onclick: startRecording }, "Record (this browser)"),
        recorder ? el("button", { class: "btn btn--danger", onclick: () => recorder && recorder.state !== "inactive" && recorder.stop() }, "Stop") : null,
        el("button", {
          class: "btn", onclick: async () => {
            const result = await api.post("/api/voice/interrupt", {});
            toast(result.stopped_playback ? "Playback stopped." : "Nothing was playing.", "ok");
          },
        }, "Interrupt (barge-in)")),
      el("div", { style: "margin-top:.4rem" }, status)),
    section("Pipeline", capsHost)),
    section("Voice turns", turnsHost),
    section("Speech history", el("div", { "data-history": "1" })));

  const history = await api.get("/api/voice/history");
  container.querySelector("[data-history]").append(table([
    { label: "when", value: (row) => fmtTime(row.at) },
    { label: "chars", value: "text_chars" },
    { label: "backend", value: "backend" },
    { label: "ok", value: (row) => (row.ok ? "yes" : `no (${row.error})`) },
    { label: "file", value: (row) => String(row.audio_path || "").split("/").pop() },
  ], history.utterances || [], { empty: "Nothing has been spoken yet." }),
    json(history, { label: "raw speech records" }));
  await refresh();
}

register({
  id: "voice", title: "Voice", icon: "🎙",
  subtitle: "microphone, VAD, speech-to-text, speech, barge-in",
  render: renderVoice,
});
