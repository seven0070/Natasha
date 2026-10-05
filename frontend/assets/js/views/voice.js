/* Voice: Living acoustic neural orb, 32-bar stereo EQ spectrum visualizer, microphone, STT, TTS, and barge-in. */

import { api } from "../api.js";
import { el, clear, fmtTime, toast } from "../ui.js";
import { section, table, stat, json } from "../components.js";
import { register } from "../router.js";

export async function renderVoice(container) {
  clear(container);

  let recorder = null;
  let chunks = [];
  let spectrumInterval = null;
  let currentAudio = null;

  // --- Header / Pipeline Specs Host ---
  const capsHost = el("div", {});
  const turnsHost = el("div", {});
  const historyHost = el("div", {});

  // --- Voice Controls Elements ---
  const textInput = el("textarea", {
    placeholder: "Type what you would say out loud, or use the acoustic microphone controls…",
    rows: 2,
    style: "width:100%;padding:0.6rem 0.85rem;background:var(--bg-lowest);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-family:inherit;font-size:13.5px;resize:vertical;",
  });
  const speakToggle = el("input", { type: "checkbox", checked: "checked" });
  const secondsInput = el("input", {
    type: "number",
    value: "5",
    min: "1",
    max: "60",
    style: "width:4.5rem;padding:0.35rem 0.5rem;background:var(--bg-lowest);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-size:13px;",
  });
  const languageInput = el("input", {
    placeholder: "en",
    value: "en",
    style: "width:4rem;padding:0.35rem 0.5rem;background:var(--bg-lowest);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-size:13px;",
  });

  // --- State Indicators ---
  const stateDot = el("span", {
    style: "width:8px;height:8px;border-radius:50%;background:var(--primary);display:inline-block;",
  });
  const stateLabel = el("span", {
    style: "font-weight:600;font-size:12px;letter-spacing:0.04em;text-transform:uppercase;",
  }, "Duplex Ready");

  const statusPill = el("div", { class: "voice-status-pill" },
    stateDot,
    stateLabel);

  // --- 32-Bar Stereo EQ Spectrum Visualizer ---
  const spectrumContainer = el("div", { class: "voice-spectrum" });
  const bars = [];
  for (let i = 0; i < 32; i++) {
    const bar = el("div", { class: "spectrum-bar", style: "height: 6px;" });
    bars.push(bar);
    spectrumContainer.append(bar);
  }

  function startSpectrumAnimation(isActive = false) {
    if (spectrumInterval) clearInterval(spectrumInterval);
    spectrumInterval = setInterval(() => {
      bars.forEach((bar, idx) => {
        let h;
        if (isActive) {
          // Dynamic bell-curve frequency simulation with noise
          const center = Math.abs(idx - 16) / 16;
          h = Math.max(6, Math.floor(Math.random() * 40 * (1 - center * 0.4) + 8));
        } else {
          // Gentle ambient resting hum
          h = Math.max(4, Math.floor(Math.random() * 8 + 4));
        }
        bar.style.height = `${h}px`;
      });
    }, 90);
  }
  startSpectrumAnimation(false);

  // --- Conversational Subtitles Display ---
  const userSub = el("div", { class: "muted", style: "font-size:14px;line-height:1.5;" }, "Waiting for acoustic input…");
  const assistantSub = el("div", { class: "bold", style: "font-size:14px;line-height:1.5;margin-top:0.35rem;color:var(--primary);" }, "Ready to synthesize responses.");

  const subtitlesCard = el("div", { class: "voice-subtitles" },
    el("div", { class: "row tight", style: "align-items:center;margin-bottom:0.4rem;" },
      el("span", { class: "chip", style: "font-size:10px;text-transform:uppercase;" }, "Latest Voice Transcript"),
      el("span", { class: "small muted" }, "Duplex Neural Stream")),
    userSub,
    el("div", { style: "height:1px;background:var(--border-soft);margin:0.6rem 0;" }),
    assistantSub);

  // --- The 3D Living Acoustic Neural Orb ---
  const shockRing = el("div", { class: "voice-shock-ring" });
  const orbitalRing = el("div", { class: "voice-orbital-ring" });
  const brandLogo = el("img", {
    src: "assets/logo.svg",
    alt: "Natasha Mark",
    class: "voice-brand-mark",
  });
  const causticGlow = el("div", { class: "voice-caustic" });

  const neuralOrb = el("div", {
    class: "voice-neural-orb",
    title: "Click to toggle listening",
    onclick: () => {
      turn({ text: "", speak: speakToggle.checked, language: languageInput.value, seconds: Number(secondsInput.value || 5) });
    },
  },
    causticGlow,
    brandLogo);

  const orbWrapper = el("div", { class: "voice-orb-wrapper" },
    shockRing,
    orbitalRing,
    neuralOrb);

  // --- Floating Ergonomic Control Dock ---
  const btnHostListen = el("button", {
    class: "btn btn--primary",
    style: "display:flex;align-items:center;gap:0.4rem;border-radius:var(--radius-pill);padding:0.45rem 0.95rem;font-size:13px;",
    onclick: () => turn({
      text: "",
      speak: speakToggle.checked,
      language: languageInput.value,
      seconds: Number(secondsInput.value || 5),
    }),
  },
    el("span", { class: "material-symbols-outlined" }, "mic"),
    "Listen (Host Mic)");

  const btnBrowserRecord = el("button", {
    class: "btn",
    style: "display:flex;align-items:center;gap:0.4rem;border-radius:var(--radius-pill);padding:0.45rem 0.95rem;font-size:13px;",
    onclick: startBrowserRecording,
  },
    el("span", { class: "material-symbols-outlined" }, "fiber_manual_record"),
    "Record (Browser Mic)");

  const btnStop = el("button", {
    class: "btn btn--danger",
    style: "display:none;align-items:center;gap:0.4rem;border-radius:var(--radius-pill);padding:0.45rem 0.95rem;font-size:13px;",
    onclick: () => {
      if (recorder && recorder.state !== "inactive") recorder.stop();
    },
  },
    el("span", { class: "material-symbols-outlined" }, "stop"),
    "Stop Recording");

  const btnInterrupt = el("button", {
    class: "btn",
    style: "display:flex;align-items:center;gap:0.4rem;border-radius:var(--radius-pill);padding:0.45rem 0.95rem;font-size:13px;",
    onclick: async () => {
      if (currentAudio) {
        currentAudio.pause();
        currentAudio = null;
      }
      try {
        const result = await api.post("/api/voice/interrupt", {});
        toast(result.stopped_playback ? "Playback interrupted (barge-in)." : "Playback stopped.", "ok");
        setState("Idle", false);
      } catch (err) {
        toast(err.message, "error");
      }
    },
  },
    el("span", { class: "material-symbols-outlined" }, "pan_tool"),
    "Interrupt (Barge-in)");

  const controlsDock = el("div", { class: "voice-controls-dock" },
    btnHostListen,
    btnBrowserRecord,
    btnStop,
    btnInterrupt);

  function setState(state, activeVisual = false) {
    stateLabel.textContent = state;
    if (activeVisual) {
      neuralOrb.classList.add("speaking");
      startSpectrumAnimation(true);
      stateDot.style.background = "var(--primary)";
    } else {
      neuralOrb.classList.remove("speaking");
      startSpectrumAnimation(false);
      stateDot.style.background = "var(--text-faint)";
    }
  }

  // --- Real Voice Backend Pipeline Actions ---
  async function refresh() {
    try {
      const payload = await api.get("/api/voice/loop");
      const caps = payload.capabilities || {};
      clear(capsHost).append(el("div", { class: "grid" },
        stat("Text to Speech", caps.text_to_speech ? "Ready" : "Unavailable",
          (caps.voice || {}).reason || "TTS Neural Synthesizer"),
        stat("Speech to Text", caps.speech_to_text ? "Ready" : "Unavailable",
          Object.keys((caps.hearing || {})).filter((k) => (caps.hearing || {})[k]).join(", ") || "Whisper Engine"),
        stat("Host Microphone", caps.microphone ? "Detected" : "Not Detected", "Audio Input Device"),
        stat("Barge-in Control", caps.barge_in ? "Supported" : "Text Only", "Real-time Interrupt")));

      clear(turnsHost).append(table([
        { label: "When", value: (row) => fmtTime(row.started_at) },
        { label: "Heard", value: (row) => row.transcript || "—" },
        { label: "Replied", value: (row) => String(row.reply || "—").slice(0, 120) },
        { label: "Speech Backend", value: (row) => row.audio_path ? `${row.speech_backend || "audio"} (${row.audio_path.split("/").pop()})` : (row.stages?.tts?.error || "text only") },
        { label: "Latency", value: (row) => `${Math.round(row.latency_ms || 0)} ms` },
      ], (payload.turns || []).slice().reverse(), { empty: "No voice turns recorded yet." }));
    } catch (err) {
      clear(capsHost).append(el("div", { class: "card", style: "padding:1rem;" },
        el("span", { class: "chip chip--warn" }, "Pipeline Offline"),
        el("p", { class: "muted small", style: "margin:0.5rem 0 0 0;" }, `Failed to query voice capabilities: ${err.message}`)));
    }
  }

  async function turn(payload) {
    setState("Listening to input…", true);
    try {
      const result = await api.post("/api/voice/turn", payload);
      if (result.ok) {
        setState("Done", false);
        userSub.textContent = `"${result.transcript || payload.text || "(Acoustic speech)"}"`;
        assistantSub.textContent = result.reply || "Awaiting next query.";
      } else {
        setState("Incomplete", false);
        if (result.error) toast(result.error, "error");
      }

      if (result.audio_path) {
        setState("Speaking reply…", true);
        try {
          if (currentAudio) currentAudio.pause();
          currentAudio = new Audio(`/api/artifacts/download?path=${encodeURIComponent(result.audio_path)}`);
          currentAudio.onended = () => setState("Idle", false);
          await currentAudio.play();
        } catch {
          setState("Idle", false);
        }
      } else {
        setState("Idle", false);
      }
      refresh();
    } catch (error) {
      setState("Failed", false);
      toast(error.message, "error");
    }
  }

  async function startBrowserRecording() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast("This browser cannot capture audio directly; use 'Listen (Host Mic)' instead.", "error");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recorder = new MediaRecorder(stream);
      chunks = [];
      recorder.ondataavailable = (event) => chunks.push(event.data);
      recorder.onstop = async () => {
        stream.getTracks().forEach((track) => track.stop());
        btnStop.style.display = "none";
        btnBrowserRecord.style.display = "flex";
        const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
        const form = new FormData();
        form.append("file", blob, "microphone.webm");
        setState("Transcribing…", true);
        try {
          const response = await fetch(`/api/voice/turn/audio?language=${encodeURIComponent(languageInput.value || "en")}&speak=${speakToggle.checked}`, {
            method: "POST",
            headers: api.token ? { "X-Natasha-Token": api.token } : {},
            body: form,
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || "voice turn failed");
          if (result.ok) {
            userSub.textContent = `"${result.transcript || "(Acoustic audio)"}"`;
            assistantSub.textContent = result.reply || "Reply generated.";
          }
          refresh();
          setState("Idle", false);
        } catch (error) {
          setState("Error", false);
          toast(error.message, "error");
        }
      };
      recorder.start();
      btnBrowserRecord.style.display = "none";
      btnStop.style.display = "flex";
      setState("Recording from browser…", true);
    } catch (error) {
      toast(`Microphone permission denied: ${error.message}`, "error");
    }
  }

  // --- Fallback Text-to-Speech Box ---
  const textSendRow = el("div", { class: "card", style: "max-width:620px;width:100%;margin-top:1.5rem;" },
    el("div", { class: "card__head" },
      el("strong", {}, "Acoustic Speech Input"),
      el("span", { class: "small muted" }, "Type or synthesize via voice loop")),
    textInput,
    el("div", { style: "display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:0.75rem;margin-top:0.75rem;" },
      el("div", { class: "row tight", style: "align-items:center;" },
        el("label", { style: "display:flex;gap:0.35rem;align-items:center;font-size:12.5px;cursor:pointer;" },
          speakToggle, "Speak reply"),
        el("span", { class: "small muted", style: "margin-left:0.5rem;" }, "Host secs:"),
        secondsInput,
        el("span", { class: "small muted", style: "margin-left:0.35rem;" }, "Lang:"),
        languageInput),
      el("button", {
        class: "btn btn--primary",
        style: "padding:0.45rem 1rem;",
        onclick: () => {
          if (!textInput.value.trim()) {
            toast("Please enter text to send through voice loop.", "error");
            return;
          }
          turn({
            text: textInput.value.trim(),
            speak: speakToggle.checked,
            language: languageInput.value,
          });
          textInput.value = "";
        },
      }, "Send Speech Turn")));

  // --- Central Voice Arena Layout ---
  const centralArena = el("div", { class: "voice-arena" },
    statusPill,
    orbWrapper,
    spectrumContainer,
    subtitlesCard,
    controlsDock,
    textSendRow);

  // --- Speech History Records ---
  try {
    const history = await api.get("/api/voice/history").catch(() => ({ utterances: [] }));
    clear(historyHost).append(
      table([
        { label: "When", value: (row) => fmtTime(row.at) },
        { label: "Characters", value: "text_chars" },
        { label: "Backend", value: "backend" },
        { label: "Success", value: (row) => (row.ok ? "yes" : `no (${row.error || "err"})`) },
        { label: "Audio File", value: (row) => String(row.audio_path || "—").split("/").pop() },
      ], history.utterances || [], { empty: "Nothing has been spoken yet." }),
      json(history, { label: "raw speech records" }));
  } catch (err) {
    clear(historyHost).append(el("div", { class: "small muted" }, "History unavailable."));
  }

  container.append(
    centralArena,
    section("Voice Pipeline Capabilities", capsHost),
    section("Recent Voice Turns", turnsHost),
    section("Speech Synthesizer History", historyHost));

  await refresh();
}

register({
  id: "voice",
  title: "Voice",
  icon: "graphic_eq",
  subtitle: "living acoustic neural orb, 32-bar stereo EQ, speech-to-text, and barge-in",
  render: renderVoice,
});
