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

const FACT_LINES = 6; // longer fact lists fold behind "Show N more"
let current = null; // AbortController of the question in flight

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

async function ask(question) {
  if (current) current.abort();
  const controller = new AbortController();
  current = controller;
  welcomeEl.hidden = true;
  show(problemEl, "");
  show(answerEl, "");
  show(statusEl, "Thinking...");
  entriesEl.replaceChildren();
  button.disabled = true;

  let answer = "";
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
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

document.addEventListener("keydown", (ev) => {
  if (ev.key === "/" && document.activeElement !== input) {
    ev.preventDefault();
    input.focus();
    input.select();
  }
});

refreshStatus();
