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
// In the Windows app the server records and plays George itself (so ` works with this window closed):
// the page then only shows what happens, and its microphone button and Voice on/off drive the server's.
let native = false;
let answerText = "";
let shownMicProblem = "";

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
    if (s.native && !native) {
      native = true;
      tellVoice();
    }
    showVoice();
    $("open-settings").classList.toggle("attention", !s.key || s.models?.state === "failed");
    micButton.disabled = Boolean(s.mic.problem);
    micButton.title = s.mic.problem || "Ask out loud: click, or hold ` (the key under Esc)";
    if (s.mic.problem) show(problemEl, s.mic.problem);
    else if (shownMicProblem && problemEl.textContent === shownMicProblem) show(problemEl, "");
    shownMicProblem = s.mic.problem;
    // Keep checking while the index is building or George's voice is downloading.
    if (!s.pages || s.building || s.models?.state === "downloading") setTimeout(refreshStatus, 3000);
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
  answerText = "";
}

// An answer's events, from its own stream (a typed question) or the server's (a question asked out loud in the app).
function showAnswerEvent(event, data) {
  if (event === "entries") {
    renderEntries(data);
    if (!data.length) show(problemEl, "No Civilopedia entries matched that question.");
  } else if (event === "delta") {
    answerText += data;
    show(statusEl, "");
    show(answerEl, answerText);
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

async function ask(question, spoken = false) {
  if (current) current.abort();
  stopSpeaking({ tell: false }); // in the app, the server stops George itself when the question arrives
  const controller = new AbortController();
  current = controller;
  clearAnswer();
  show(statusEl, "Thinking...");
  button.disabled = true;
  const speak = speakAnswers && !voiceProblem;

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
        if (event === "say") {
          if (current === controller && !native) queueSpeech(data.url); // in the app, the server plays it
        } else {
          showAnswerEvent(event, data);
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

function stopSpeaking({ tell = true } = {}) {
  if (native && tell) post("/api/hush", {});
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

function tellVoice() {
  // The app's server reads answers to questions asked out loud with this page closed too: it needs to know.
  if (native) post("/api/voice", { on: speakAnswers });
}

voiceButton.addEventListener("click", () => {
  speakAnswers = !speakAnswers;
  localStorage.setItem("civspeak.speak", speakAnswers ? "on" : "off");
  tellVoice();
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

// George and Whisper are unloaded after a while unused: ask the server to load them again as soon
// as a question starts, so they are ready by the time it is asked.
let lastWake = 0;
function wake() {
  if (performance.now() - lastWake < 20000) return;
  lastWake = performance.now();
  post("/api/wake", {});
}

function post(path, body) {
  return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .catch(() => {});
}

function setMic(state) {
  rec.state = state;
  micButton.dataset.state = state;
  micButton.setAttribute("aria-pressed", String(state === "listening"));
  micButton.setAttribute("aria-label", state === "listening" ? "Stop listening" : "Ask out loud");
}

function notAllowedMessage() {
  // In the Civspeak app (its own window, which pywebview gives a window.pywebview) only Windows itself can say no.
  if ("pywebview" in window) {
    return "Windows is not letting Civspeak use the microphone. Turn on Settings > Privacy & security > Microphone > " +
      "Let desktop apps access your microphone, then try again.";
  }
  return "The browser is not allowed to use the microphone. Allow it for this page (the icon at the left of the address bar) and try again.";
}

async function startListening(mode) {
  if (rec.state !== "idle" || micButton.disabled) return;
  wake();
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
      ? notAllowedMessage()
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
// helper) all work the same way: hold to talk, or tap to talk until you stop. In the app the server
// keeps these same rules for its own microphone.
function pressTalk() {
  if (native) return void post("/api/mic", { key: "down" });
  rec.pressedAt = performance.now();
  if (rec.state === "listening" && rec.mode === "toggle") finishListening();
  else if (rec.state === "idle") startListening("hold");
}

function releaseTalk() {
  if (native) return void post("/api/mic", { key: "up" });
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
    if (native) {
      // A tap: start listening until the talking stops, or stop listening now.
      const listening = rec.state === "listening";
      post("/api/mic", { key: "down" }).then(() => listening || post("/api/mic", { key: "up" }));
    } else if (rec.state === "listening") finishListening();
    else startListening("toggle");
  }
});

// -- Typing and keys -------------------------------------------------------------------------

input.addEventListener("input", wake);

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

document.addEventListener("pointerdown", () => native || audioContext(), { capture: true });

document.addEventListener("keydown", (ev) => {
  if (settingsDialog.open) return; // typing in Settings: ` and Esc are the dialog's own
  if (!native) audioContext();
  if (ev.code === "Backquote" && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
    // Hold ` to talk (tap it to talk until you stop); it never types into the box.
    ev.preventDefault();
    if (!ev.repeat) pressTalk();
  } else if (ev.key === "Escape") {
    if (native && rec.state === "listening") post("/api/mic", { key: "cancel" });
    else if (rec.state === "listening" || rec.state === "starting") cancelListening();
    else stopSpeaking();
  } else if (ev.key === "/" && document.activeElement !== input) {
    ev.preventDefault();
    input.focus();
    input.select();
  }
});

document.addEventListener("keyup", (ev) => {
  if (ev.code !== "Backquote" || settingsDialog.open) return;
  ev.preventDefault();
  releaseTalk();
});

// ` held while a game has the focus: the Windows helper tells the server, the server tells us.
const events = new EventSource("/api/talk");
events.addEventListener("talk", (ev) => {
  const { key } = JSON.parse(ev.data);
  if (key === "down") pressTalk();
  else releaseTalk();
});

// In the app: the server's own microphone, its answers to spoken questions and George speaking.
const WORKING = "Working out what you said...";
const on = (name, handle) => events.addEventListener(name, (ev) => handle(JSON.parse(ev.data)));

on("mic", ({ state, mode }) => {
  if (micButton.disabled) refreshStatus(); // a microphone plugged in since the page said there was none
  const was = rec.state;
  rec.mode = mode;
  setMic(state);
  if (state === "listening") {
    if (was !== "listening") {
      if (current) current.abort();
      stopSpeaking({ tell: false });
      welcomeEl.hidden = true;
      show(problemEl, "");
    }
    showListening();
  } else if (state === "working") {
    clearAnswer();
    input.value = "";
    show(statusEl, WORKING);
  } else {
    micButton.style.setProperty("--level", "0");
    if (was === "listening" || statusEl.textContent === WORKING) show(statusEl, "");
  }
});
on("level", ({ level }) => micButton.style.setProperty("--level", level.toFixed(3)));
on("question", ({ text }) => {
  if (current) current.abort();
  current = null;
  button.disabled = false;
  clearAnswer();
  input.value = text;
  show(statusEl, "Thinking...");
});
for (const name of ["entries", "delta", "done", "problem"]) on(name, (data) => showAnswerEvent(name, data));
on("speaking", ({ on: speaking }) => setSpeaking(speaking));

// -- Settings, and the first-run screen ------------------------------------------------------
// The same dialog: on the Windows app's first run it opens by itself as "Set up Civspeak".

const settingsDialog = $("settings");
let settingsView = null;
let settingsTimer = 0;
let setupMode = false;

function mb(n) {
  return `${Math.round(n / 1e6)} MB`;
}

function note(id, text, kind = "") {
  const el = $(id);
  show(el, text);
  el.className = `note ${kind}`.trim();
}

function mark(id, ok) {
  $(id).classList.toggle("ok", ok === true);
  $(id).classList.toggle("bad", ok === false);
}

function renderSettings(v) {
  settingsView = v;
  $("console-link").href = v.console_url;

  const key = v.key.present;
  mark("set-key", key);
  $("key-state").textContent = key
    ? `Your key is saved${v.key.where.includes("Credential") ? " in Windows Credential Manager" : ` in ${v.key.where}`}. Paste a new one to replace it.`
    : "Civspeak asks Claude to answer with your own key. Getting one takes a couple of minutes:";
  $("key-steps").hidden = key;
  $("key-remove").hidden = !key;
  $("key-save").textContent = key ? "Check and replace" : "Check and save";

  const game = v.game;
  mark("set-game", game.found ? true : false);
  $("game-state").textContent = game.found
    ? `${game.chosen ? "Using" : "Found"} ${game.found}.`
    : game.problem;
  $("game-state").classList.toggle("bad", !game.found);
  const english = !game.language || game.language.toLowerCase().startsWith("en");
  show($("game-language"), english ? "" :
    `Civspeak speaks English. Your game shows ${game.language}, so George uses the English names and text.`);
  // The Windows app shows Windows' own folder picker; a browser page takes the folder typed in.
  $("game-choose").hidden = !v.can_choose_folder;
  $("game-input").hidden = $("game-save").hidden = v.can_choose_folder || game.missing === "user_dir";
  if (game.missing === "user_dir") $("game-choose").hidden = true;
  $("game-auto").hidden = !game.chosen;
  if (!game.found && !$("game-input").value) $("game-input").value = game.chosen || "";

  const m = v.models;
  $("set-models").hidden = !m;
  if (m) {
    mark("set-models", m.state === "ready" ? true : m.state === "failed" ? false : null);
    const label = {
      ready: "Ready. They are kept on this PC, so updates never download them again.",
      downloading: `Downloading, ${mb(m.done)} of ${mb(m.total)}. Typed questions work meanwhile.`,
      missing: `Not downloaded yet (${mb(m.total)}).`,
      failed: `The download stopped: ${m.error}`,
    }[m.state];
    $("models-state").textContent = label;
    $("models-state").classList.toggle("bad", m.state === "failed");
    $("models-progress").hidden = m.state === "ready";
    $("models-progress").value = m.total ? m.done / m.total : 0;
    $("models-retry").hidden = m.state !== "failed" && m.state !== "missing";
  }

  const cap = v.cap;
  if (document.activeElement !== $("cap-input")) $("cap-input").value = Number(cap.usd).toFixed(2).replace(/\.00$/, "");
  $("cap-input").disabled = cap.from_environment;
  note("cap-note", cap.from_environment
    ? `Set to ${money(cap.usd)} by CIVSPEAK_MONTHLY_CAP_USD on this PC. ${money(cap.spent)} spent this month.`
    : `${money(cap.spent)} spent this month. Civspeak stops asking Claude when the limit is reached. ` +
      "For a second safety net, set a spend limit in the Claude Console as well.");

  $("set-login").hidden = v.start_at_login === null;
  $("login-box").checked = Boolean(v.start_at_login);

  clearTimeout(settingsTimer);
  if (settingsDialog.open && m && m.state === "downloading") settingsTimer = setTimeout(loadSettings, 1000);
}

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    renderSettings(await res.json());
  } catch {
    // Civspeak went away; the header already says so.
  }
}

async function postSettings(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  if (out.settings) renderSettings(out.settings);
  return out.problem ?? out.error ?? "";
}

function openSettings(setup = false) {
  setupMode = setup;
  $("settings-title").textContent = setup ? "Set up Civspeak" : "Settings";
  $("settings-lead").hidden = !setup;
  $("settings-done").textContent = setup ? "Start using Civspeak" : "Done";
  for (const id of ["key-note", "game-note"]) note(id, "");
  if (!settingsDialog.open) settingsDialog.showModal();
  loadSettings();
}

function closeSettings() {
  clearTimeout(settingsTimer);
  if (settingsDialog.open) settingsDialog.close();
}

settingsDialog.addEventListener("close", () => {
  clearTimeout(settingsTimer);
  if (setupMode) postSettings("/api/settings", { setup_done: true }); // finished or skipped: it does not come back
  setupMode = false;
  refreshStatus();
  input.focus();
});
settingsDialog.addEventListener("click", (ev) => {
  if (ev.target === settingsDialog) closeSettings(); // a click on the dimmed backdrop
});

$("open-settings").addEventListener("click", () => openSettings());
$("settings-close").addEventListener("click", closeSettings);
$("settings-done").addEventListener("click", closeSettings);

async function busy(button, work) {
  button.disabled = true;
  try {
    return await work();
  } catch {
    return "Could not reach Civspeak. Is it still running?";
  } finally {
    button.disabled = false;
  }
}

async function saveKey() {
  const field = $("key-input");
  note("key-note", "Checking the key with Claude (this costs nothing)...");
  const problem = await busy($("key-save"), () => postSettings("/api/settings/key", { key: field.value }));
  if (problem) {
    note("key-note", problem, "bad");
  } else {
    field.value = "";
    note("key-note", "Claude accepted the key. It is saved.", "good");
  }
}
$("key-save").addEventListener("click", saveKey);
$("key-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") saveKey();
});
$("key-remove").addEventListener("click", async () => {
  const problem = await busy($("key-remove"), () => postSettings("/api/settings/key", { remove: true }));
  note("key-note", problem || "The key is removed from this PC.", problem ? "bad" : "");
});

async function useFolder(work, button) {
  note("game-note", "");
  const problem = await busy(button, work);
  if (problem) note("game-note", problem, "bad");
  else if (settingsView?.game.found) note("game-note", "Civspeak is reading the Civilopedia from it now.", "good");
}
$("game-choose").addEventListener("click", () => useFolder(() => postSettings("/api/settings/folder", {}), $("game-choose")));
$("game-save").addEventListener("click", () =>
  useFolder(() => postSettings("/api/settings", { game_dir: $("game-input").value }), $("game-save")));
$("game-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") $("game-save").click();
});
$("game-auto").addEventListener("click", async () => {
  $("game-input").value = "";
  await useFolder(() => postSettings("/api/settings", { game_dir: "" }), $("game-auto"));
});

$("models-retry").addEventListener("click", async () => {
  await busy($("models-retry"), async () => {
    await post("/api/models", {});
    await loadSettings();
    return "";
  });
});

$("cap-input").addEventListener("change", async () => {
  const value = Number($("cap-input").value);
  if (!(value > 0)) return void loadSettings();
  const problem = await postSettings("/api/settings", { cap_usd: Math.round(value * 100) / 100 });
  if (problem) note("cap-note", problem, "bad");
  else setSpend(settingsView.cap.spent, settingsView.cap.usd);
});

$("login-box").addEventListener("change", () => postSettings("/api/settings", { start_at_login: $("login-box").checked }));

showVoice();

refreshStatus();
fetch("/api/settings")
  .then((res) => res.json())
  .then((v) => {
    if (v.setup_needed) openSettings(true);
  })
  .catch(() => {});
