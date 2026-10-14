"use strict";

const $ = (id) => document.getElementById(id);
const form = $("ask");
const input = $("question");
const button = $("go");
const statusEl = $("status");
const answerEl = $("answer");
const problemEl = $("problem");
const entriesEl = $("entries");
const welcomeEl = $("welcome");
const cardTemplate = $("card");
const micButton = $("mic");
const voiceButton = $("voice");
const speakingEl = $("speaking");

const FACT_LINES = 6; // longer fact lists fold behind "Show N more"
let current = null; // AbortController of the question in flight
let speakAnswers = localStorage.getItem("civspeak.speak") !== "off";
let voiceProblem = "";

function show(el, text) {
  el.textContent = text;
  el.hidden = !text;
}

function money(n) {
  return "$" + Number(n).toFixed(2);
}

function setSpend(spent, cap) {
  const el = $("spend");
  el.hidden = false;
  el.textContent = `${money(spent)} of ${money(cap)} this month`;
  el.classList.toggle("warn", spent >= cap * 0.9);
}

async function refreshStatus() {
  try {
    const res = await fetch("/api/status");
    const s = await res.json();
    const source = $("source");
    source.classList.remove("warn");
    if (s.pages) {
      source.textContent = `${s.pages.toLocaleString()} entries, from the game you loaded at ${s.game_loaded_label}` +
        (s.building ? " (updating...)" : "");
    } else if (s.build_error) {
      source.textContent = s.build_error;
      source.classList.add("warn");
    } else {
      source.textContent = "Building the Civilopedia from your game...";
    }
    setSpend(s.spent, s.cap);
    voiceProblem = s.voice.problem;
    showVoice();
    micButton.disabled = Boolean(s.mic.problem);
    micButton.title = s.mic.problem || "Ask out loud: click, or hold ` (the key under Esc)";
    if (s.mic.problem) show(problemEl, s.mic.problem);
    if (!s.pages || s.building) setTimeout(refreshStatus, 3000);
  } catch {
    $("source").textContent = "Civspeak is not running.";
    $("source").classList.add("warn");
  }
}

function renderEntries(entries) {
  entriesEl.replaceChildren();
  entries.forEach((e, i) => {
    const card = cardTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.number = String(i + 1);
    card.querySelector(".title").textContent = e.title;
    card.querySelector(".where").textContent = e.group ? `${e.section} · ${e.group}` : e.section;

    const facts = card.querySelector(".facts");
    for (const g of e.facts) {
      const dt = document.createElement("dt");
      dt.textContent = g.heading;
      const dd = document.createElement("dd");
      if (g.lines.length > 1) {
        const ul = document.createElement("ul");
        g.lines.forEach((line, n) => {
          const li = document.createElement("li");
          li.textContent = line;
          li.hidden = n >= FACT_LINES;
          ul.append(li);
        });
        dd.append(ul);
        if (g.lines.length > FACT_LINES) {
          const more = document.createElement("button");
          more.type = "button";
          more.className = "more";
          more.textContent = `Show ${g.lines.length - FACT_LINES} more`;
          more.addEventListener("click", () => {
            for (const li of ul.children) li.hidden = false;
            more.remove();
          });
          dd.append(more);
        }
      } else {
        dd.textContent = g.lines[0];
      }
      facts.append(dt, dd);
    }
    if (!e.facts.length) facts.remove();

    const chapters = card.querySelector(".chapters");
    e.chapters.forEach((c, n) => {
      // The first chapter (usually the description) is open; history and the rest fold away.
      const open = n === 0;
      const box = document.createElement(open ? "div" : "details");
      box.className = "chapter";
      const head = document.createElement(open ? "h3" : "summary");
      head.textContent = c.title || "More";
      box.append(head);
      for (const para of c.paragraphs) {
        const p = document.createElement("p");
        p.textContent = para;
        box.append(p);
      }
      chapters.append(box);
    });
    entriesEl.append(card);
  });
}

function markUsed(used) {
  for (const card of entriesEl.children) {
    const isUsed = used.includes(Number(card.dataset.number));
    card.classList.toggle("used", isUsed);
    card.querySelector(".used-tag").hidden = !isUsed;
  }
  // Put the entries the answer used first.
  const cards = [...entriesEl.children];
  cards.sort((a, b) => (b.classList.contains("used") - a.classList.contains("used")) ||
    (Number(a.dataset.number) - Number(b.dataset.number)));
  entriesEl.replaceChildren(...cards);
}

function clearAnswer() {
  welcomeEl.hidden = true;
  show(problemEl, "");
  show(answerEl, "");
  entriesEl.replaceChildren();
}

async function ask(question, spoken = false) {
  if (current) current.abort();
  stopSpeaking();
  const controller = new AbortController();
  current = controller;
  clearAnswer();
  show(statusEl, "Thinking...");
  button.disabled = true;
  const speak = speakAnswers && !voiceProblem;

  let answer = "";
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, speak, spoken }),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`The server said ${res.status}.`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const event = /^event: (.*)$/m.exec(raw)?.[1];
        const data = JSON.parse(/^data: (.*)$/m.exec(raw)?.[1] ?? "null");
        if (event === "entries") {
          renderEntries(data);
          if (!data.length) show(problemEl, "No Civilopedia entries matched that question.");
        } else if (event === "delta") {
          answer += data;
          show(statusEl, "");
          show(answerEl, answer);
        } else if (event === "say") {
          if (current === controller) queueSpeech(data.url);
        } else if (event === "done") {
          show(statusEl, "");
          show(answerEl, data.text);
          markUsed(data.used);
          setSpend(data.spent, data.cap);
        } else if (event === "problem") {
          show(statusEl, "");
          show(problemEl, data.message);
        }
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      show(statusEl, "");
      show(problemEl, "Could not reach Civspeak. Is it still running?");
    }
  } finally {
    if (current === controller) {
      current = null;
      button.disabled = false;
      show(statusEl, "");
    }
  }
}

// -- George speaking ------------------------------------------------------------------------
// Each sentence arrives as its own WAV as soon as it is voiced; they play back to back.

const player = { ctx: null, queue: [], source: null, generation: 0, running: false };

function audioContext() {
  // Created and resumed on a click or key press, so the browser lets it play.
  if (!player.ctx) player.ctx = new AudioContext();
  if (player.ctx.state === "suspended") player.ctx.resume();
  return player.ctx;
}

function queueSpeech(url) {
  const ctx = audioContext();
  const clip = fetch(url)
    .then((res) => (res.ok ? res.arrayBuffer() : null))
    .then((bytes) => (bytes ? ctx.decodeAudioData(bytes) : null))
    .catch(() => null);
  player.queue.push(clip);
  if (!player.running) playQueue(player.generation);
}

async function playQueue(generation) {
  player.running = true;
  while (player.queue.length && generation === player.generation) {
    const buffer = await player.queue.shift();
    if (generation !== player.generation) break;
    if (!buffer) continue;
    setSpeaking(true);
    await new Promise((resolve) => {
      const source = player.ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(player.ctx.destination);
      source.onended = resolve;
      player.source = source;
      source.start();
    });
    player.source = null;
  }
  if (generation === player.generation) {
    player.running = false;
    setSpeaking(false);
  }
}

function stopSpeaking() {
  player.generation += 1;
  player.queue = [];
  player.running = false;
  if (player.source) {
    player.source.onended = null;
    try { player.source.stop(); } catch { /* already stopped */ }
    player.source = null;
  }
  setSpeaking(false);
}

function setSpeaking(on) {
  speakingEl.hidden = !on;
  document.body.classList.toggle("is-speaking", on);
}

function showVoice() {
  const on = speakAnswers && !voiceProblem;
  voiceButton.setAttribute("aria-pressed", String(on));
  voiceButton.textContent = voiceProblem ? "Voice unavailable" : on ? "Voice on" : "Voice off";
  voiceButton.title = voiceProblem || (on ? "George reads answers out. Click to turn off." : "Click to have answers read out.");
  voiceButton.classList.toggle("warn", Boolean(voiceProblem));
  voiceButton.disabled = Boolean(voiceProblem);
}

voiceButton.addEventListener("click", () => {
  speakAnswers = !speakAnswers;
  localStorage.setItem("civspeak.speak", speakAnswers ? "on" : "off");
  if (!speakAnswers) stopSpeaking();
  showVoice();
});

$("stop-speaking").addEventListener("click", stopSpeaking);

// -- The microphone --------------------------------------------------------------------------
// Click the mic (or tap `) and ask: it stops by itself when you stop talking. Or hold the mic
// button or the ` key while you talk and let go when done. The recording goes to the server as a
// 16 kHz WAV, which is what Whisper wants.

const SPEECH_LEVEL = 0.012; // RMS above this counts as talking
const END_SILENCE_MS = 1200; // this much quiet after talking ends the question
const NOTHING_SAID_MS = 6000; // give up if nobody talks for this long
const MAX_RECORDING_MS = 15000;
const HOLD_MS = 350; // a press longer than this is hold-to-talk

const rec = { state: "idle", mode: "toggle", stream: null, ctx: null, chunks: [], started: 0, lastVoice: 0, heard: false, pressedAt: 0 };

function setMic(state) {
  rec.state = state;
  micButton.dataset.state = state;
  micButton.setAttribute("aria-pressed", String(state === "listening"));
  micButton.setAttribute("aria-label", state === "listening" ? "Stop listening" : "Ask out loud");
}

async function startListening(mode) {
  if (rec.state !== "idle" || micButton.disabled) return;
  if (current) current.abort();
  stopSpeaking();
  audioContext();
  rec.mode = mode;
  rec.chunks = [];
  rec.heard = false;
  setMic("starting");
  welcomeEl.hidden = true;
  show(problemEl, "");
  try {
    rec.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    rec.ctx = new AudioContext({ sampleRate: 16000 });
    await rec.ctx.audioWorklet.addModule("/recorder.js");
    // A page nobody has clicked yet may not use sound (the helper's own window is allowed to).
    if (rec.ctx.state !== "running") await Promise.race([rec.ctx.resume(), new Promise((r) => setTimeout(r, 300))]);
    if (rec.ctx.state !== "running") throw Object.assign(new Error("not allowed to start sound yet"), { name: "NeedsClick" });
    if (rec.state !== "starting") throw new Error("cancelled"); // Esc while the mic was starting
    const node = new AudioWorkletNode(rec.ctx, "recorder");
    node.port.onmessage = (ev) => onSamples(ev.data);
    rec.ctx.createMediaStreamSource(rec.stream).connect(node);
    node.connect(rec.ctx.destination); // silent; keeps the recorder running
  } catch (err) {
    releaseMic();
    if (rec.state !== "starting") return;
    setMic("idle");
    show(statusEl, "");
    show(problemEl, err && err.name === "NotAllowedError"
      ? "The browser is not allowed to use the microphone. Allow it for this page (the icon at the left of the address bar) and try again."
      : err && err.name === "NeedsClick"
        ? "Click anywhere on this page once so it may use the microphone and sound, then ask again."
        : "Could not start the microphone. Is one plugged in?");
    return;
  }
  rec.started = rec.lastVoice = performance.now();
  setMic("listening");
  showListening();
  if (rec.releasedEarly) finishListening();
}

function showListening() {
  show(statusEl, rec.mode === "hold" ? "Listening... let go when you're done" : "Listening... ask your question");
}

function onSamples({ samples, rms }) {
  if (rec.state !== "listening") return;
  rec.chunks.push(samples);
  const now = performance.now();
  micButton.style.setProperty("--level", Math.min(1, rms / 0.08).toFixed(3));
  if (rms > SPEECH_LEVEL) {
    rec.heard = true;
    rec.lastVoice = now;
  }
  if (now - rec.started > MAX_RECORDING_MS) finishListening();
  else if (rec.mode === "toggle" && rec.heard && now - rec.lastVoice > END_SILENCE_MS) finishListening();
  else if (rec.mode === "toggle" && !rec.heard && now - rec.started > NOTHING_SAID_MS) finishListening();
}

function releaseMic() {
  if (rec.stream) for (const t of rec.stream.getTracks()) t.stop();
  if (rec.ctx) rec.ctx.close();
  rec.stream = rec.ctx = null;
  rec.releasedEarly = false;
  micButton.style.setProperty("--level", "0");
}

function cancelListening() {
  releaseMic();
  setMic("idle");
  show(statusEl, "");
}

async function finishListening() {
  if (rec.state === "starting") {
    rec.releasedEarly = true; // let go before the mic was ready: stop as soon as it is
    return;
  }
  if (rec.state !== "listening") return;
  const rate = rec.ctx.sampleRate;
  const chunks = rec.chunks;
  const heard = rec.heard;
  releaseMic();
  if (!heard) {
    setMic("idle");
    show(statusEl, "");
    show(problemEl, "I didn't hear anything. Click the microphone and ask again.");
    return;
  }
  setMic("working");
  clearAnswer();
  input.value = "";
  show(statusEl, "Working out what you said...");
  try {
    const res = await fetch(`/api/listen?speak=${speakAnswers && !voiceProblem ? 1 : 0}`, {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wav(chunks, rate),
    });
    const reply = await res.json();
    if (!res.ok) throw new Error(reply.error || `The server said ${res.status}.`);
    setMic("idle");
    if (!reply.text) {
      show(statusEl, "");
      show(problemEl, "Sorry, I didn't catch that. Try again a little closer to the microphone.");
      if (reply.say) queueSpeech(reply.say.url);
      return;
    }
    input.value = reply.text;
    ask(reply.text, true);
  } catch (err) {
    setMic("idle");
    show(statusEl, "");
    show(problemEl, err.message.startsWith("The ") ? err.message : "Could not reach Civspeak. Is it still running?");
  }
}

function wav(chunks, rate) {
  const length = chunks.reduce((n, c) => n + c.length, 0);
  const view = new DataView(new ArrayBuffer(44 + length * 2));
  const text = (offset, s) => { for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i)); };
  text(0, "RIFF"); view.setUint32(4, 36 + length * 2, true); text(8, "WAVE");
  text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, "data"); view.setUint32(40, length * 2, true);
  let offset = 44;
  for (const c of chunks) {
    for (const s of c) {
      view.setInt16(offset, Math.max(-1, Math.min(1, s)) * 0x7fff, true);
      offset += 2;
    }
  }
  return view.buffer;
}

// Pressing and letting go of the mic button, the ` key, or ` held in a game (through the Windows
// helper) all work the same way: hold to talk, or tap to talk until you stop.
function pressTalk() {
  rec.pressedAt = performance.now();
  if (rec.state === "listening" && rec.mode === "toggle") finishListening();
  else if (rec.state === "idle") startListening("hold");
}

function releaseTalk() {
  if (rec.mode !== "hold" || (rec.state !== "listening" && rec.state !== "starting")) return;
  if (performance.now() - rec.pressedAt < HOLD_MS) rec.mode = "toggle"; // a tap: keep listening
  else finishListening();
  if (rec.state === "listening") showListening();
}

micButton.addEventListener("pointerdown", (ev) => {
  if (ev.button === 0) pressTalk();
});

micButton.addEventListener("pointerup", releaseTalk);

micButton.addEventListener("keydown", (ev) => {
  // Enter or Space on the focused button behaves like a click.
  if ((ev.key === "Enter" || ev.key === " ") && !ev.repeat) {
    ev.preventDefault();
    if (rec.state === "listening") finishListening();
    else startListening("toggle");
  }
});

// -- Typing and keys -------------------------------------------------------------------------

form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = input.value.trim();
  if (q) ask(q);
});

for (const b of document.querySelectorAll(".try")) {
  b.addEventListener("click", () => {
    input.value = b.textContent;
    ask(b.textContent);
  });
}

document.addEventListener("pointerdown", audioContext, { capture: true });

document.addEventListener("keydown", (ev) => {
  audioContext();
  if (ev.code === "Backquote" && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
    // Hold ` to talk (tap it to talk until you stop); it never types into the box.
    ev.preventDefault();
    if (!ev.repeat) pressTalk();
  } else if (ev.key === "Escape") {
    if (rec.state === "listening" || rec.state === "starting") cancelListening();
    else stopSpeaking();
  } else if (ev.key === "/" && document.activeElement !== input) {
    ev.preventDefault();
    input.focus();
    input.select();
  }
});

document.addEventListener("keyup", (ev) => {
  if (ev.code !== "Backquote") return;
  ev.preventDefault();
  releaseTalk();
});

// ` held while a game has the focus: the Windows helper tells the server, the server tells us.
new EventSource("/api/talk").addEventListener("talk", (ev) => {
  const { key } = JSON.parse(ev.data);
  if (key === "down") pressTalk();
  else releaseTalk();
});

showVoice();

refreshStatus();
