// Civ Speak's website: the paintings in the hero, and the Ask it yourself demo.
// Nothing here makes a sound until Ask is pressed. There are no sound files: the answer is read
// by the browser's own speech.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------------------------------------------------------------- the paintings
  const AGES = [
    { file: "1-ancient",
      alt: "A painting of a valley at dawn. An old storyteller sits on a rock by a fire with two children listening. Behind are clay granaries, a palm tree, and a stepped temple across the river." },
    { file: "2-classical",
      alt: "A painting of the valley in the morning. A bearded philosopher in a white robe raises one arm while a young man listens from a marble block. Behind are a columned porch, an olive tree, a temple on a rock and an aqueduct." },
    { file: "3-medieval",
      alt: "A painting of the valley by day. A monk in a brown habit reads aloud from a book on a lectern by a tall candle, while another monk listens on a bench. Behind are a stone chapel with a coloured window, an oak, a castle and a windmill." },
    { file: "4-renaissance",
      alt: "A painting of the valley in golden light. A town crier in a red coat stands on a box ringing a bell and holding a long scroll, while a woman and a child listen. Behind are a timbered house, a pennant, a great dome, a bell tower and a ship." },
    { file: "5-industrial",
      alt: "A painting of the valley at dusk. A clerk in a bowler hat taps a telegraph key at a desk under a green lamp. Behind are a wooden telegraph hut, a pole with wires, a works with two smoking chimneys, and a train on a viaduct." },
    { file: "6-modern",
      alt: "A painting of the valley at sunset. A radio announcer in a suit reads from a page into a microphone, while a listener sits in an armchair by a glowing wireless set. Behind are a small station with an On Air sign, a city of lit towers, a bridge and an airship." },
    { file: "7-atomic",
      alt: "A painting of the valley at twilight. A controller in a headset sits at a console with two green screens. Behind are a concrete blockhouse, a red and white mast, a rocket on its pad and a large dish." },
    { file: "8-information",
      alt: "A painting of the valley at night. Someone in a headset sits at a desk, talking to a screen that shows a gold star and a sound wave. A cat watches. Behind are a house with a lit glass wall and a city of glass towers." },
  ];

  const art = $("art");
  const pictures = [$("art-first")];
  let age = 0;

  function picture(i) {
    if (!pictures[i]) {
      const img = document.createElement("img");
      img.className = "age-img";
      img.width = 2560; img.height = 800;
      img.decoding = "async";
      img.src = "img/ages/" + AGES[i].file + ".webp";
      art.appendChild(img);
      pictures[i] = img;
    }
    pictures[i].alt = AGES[i].alt;
    return pictures[i];
  }

  function showAge(i) {
    age = (i + AGES.length) % AGES.length;
    const next = picture(age);
    pictures.forEach((img) => { if (img) img.classList.toggle("on", img === next); });
    // have the neighbours ready, so the arrows feel quick
    setTimeout(() => { picture((age + 1) % AGES.length); picture((age + AGES.length - 1) % AGES.length); }, 600);
  }

  $("age-prev").addEventListener("click", () => showAge(age - 1));
  $("age-next").addEventListener("click", () => showAge(age + 1));
  showAge(0);

  // ---------------------------------------------------------------- the voice
  const synth = window.speechSynthesis || null;
  let voices = [];
  const loadVoices = () => { voices = synth ? synth.getVoices() : []; };
  if (synth) {
    loadVoices();
    if (synth.addEventListener) synth.addEventListener("voiceschanged", loadVoices);
  }

  // The nearest thing the device has to George: a British voice of that name, then any British
  // man's voice, then any English one.
  function georgeVoice() {
    const english = voices.filter((v) => /^en([-_]|$)/i.test(v.lang));
    const british = english.filter((v) => /^en[-_]GB/i.test(v.lang));
    const pool = british.length ? british : english;
    return pool.find((v) => /george/i.test(v.name))
      || pool.find((v) => /(ryan|daniel|arthur|oliver|thomas|brian|\bmale)/i.test(v.name) && !/female/i.test(v.name))
      || pool[0]
      || null;
  }

  const RATE = 0.98;
  const spokenFor = (text) => text.length * 68 / RATE + 500;
  let turn = 0; // goes up whenever a new answer starts, so the one before knows to stop

  function say(text, onword) {
    if (!synth || !window.SpeechSynthesisUtterance) return wait(spokenFor(text));
    return new Promise((done) => {
      const u = new SpeechSynthesisUtterance(text);
      const voice = georgeVoice();
      if (voice) { u.voice = voice; u.lang = voice.lang; } else { u.lang = "en-GB"; }
      u.rate = RATE;
      u.pitch = 0.9;
      let over = false;
      const finish = () => { if (!over) { over = true; done(); } };
      u.onboundary = (e) => onword(e.charIndex);
      u.onend = finish;
      u.onerror = finish;
      setTimeout(finish, spokenFor(text) * 2 + 4000); // some browsers never say they have finished
      synth.speak(u);
    });
  }

  function hush() {
    turn++;
    if (synth) synth.cancel();
  }

  // ---------------------------------------------------------------- Ask it yourself
  // A handful of answers, written for this page. The app itself reads the Civilopedia from the
  // player's own game, so it can answer anything in it; a web page has no game to read.
  const ANSWERS = [
    { q: "What does a Granary do?", match: /granar/i, entries: ["Granary", "Pottery"],
      a: "It gives the city 1 Food and 2 Housing. You need Pottery to build it." },
    { q: "How do I get Great Prophets?", match: /prophet/i, entries: ["Great Prophet", "Holy Site", "Stonehenge"],
      a: "Earn Great Prophet points. Holy Sites and their buildings give you some every turn. Stonehenge hands you one outright." },
    { q: "What does a Ziggurat do?", match: /ziggurat/i, entries: ["Ziggurat", "Sumeria"],
      a: "It is Sumeria's own tile improvement. It gives 2 Science, and 1 Culture as well if it sits beside a river. It cannot go on hills." },
    { q: "Where can I build a Mbanza?", match: /mbanza/i, entries: ["Mbanza", "Kongo", "Guilds"],
      a: "Only on Woods or Rainforest. It is Kongo's own district. It takes the place of the Neighborhood and comes early, with Guilds." },
    { q: "What do I need for a Spaceport?", match: /space\s?port/i, entries: ["Spaceport", "Rocketry"],
      a: "The Rocketry technology, and a flat tile to put it on. It is the district where you build the space race projects." },
    { q: "What does Stonehenge do?", match: /stonehenge/i, entries: ["Stonehenge", "Astrology"],
      a: "It hands you a Great Prophet as soon as it is built. It has to go on flat land next to Stone, and you need Astrology first." },
  ];
  const FIRST = ANSWERS[1];
  const UNKNOWN = "This page only knows a handful of answers. In the game, I can answer anything in your Civilopedia.";
  const QUILL = '<svg viewBox="0 0 44 44" aria-hidden="true"><path d="M5,40 L11,27 C15,14 26,6 40,4 C38,18 29,28 16,32 L10,33Z" fill="#fff8e6" stroke="#1b1f4b" stroke-width="1.6" stroke-linejoin="round"/><path d="M4,41 L20,23 M17,20 l8,1 M14,25 l9,1 M21,15 l8,1" stroke="#1b1f4b" stroke-width="1.4" stroke-linecap="round" fill="none"/></svg>';

  const scroll = $("scroll");
  const scrollQ = $("scroll-q");
  const scrollA = $("scroll-a");
  const scrollStatus = $("scroll-status");
  const entries = $("entries");
  const tryInput = $("try-q");

  const wave = scroll.querySelector(".wave");
  for (let i = 0; i < 26; i++) {
    const bar = document.createElement("i");
    bar.style.setProperty("--h", 6 + Math.round(Math.random() * 18) + "px");
    bar.style.animationDelay = Math.round(Math.random() * 600) + "ms";
    wave.appendChild(bar);
  }

  // Shows the answer written as far as `upTo` characters, with the quill where the writing has got to.
  function written(text, upTo) {
    let cut = Math.min(text.length, Math.max(0, upTo));
    if (cut < text.length) {
      const space = text.indexOf(" ", cut);
      cut = space === -1 ? text.length : space;
    }
    scrollA.textContent = "";
    const done = document.createElement("span");
    done.textContent = text.slice(0, cut);
    scrollA.appendChild(done);
    if (cut < text.length) {
      const quill = document.createElement("span");
      quill.className = "quill";
      quill.innerHTML = QUILL;
      const todo = document.createElement("span");
      todo.className = "todo";
      todo.textContent = text.slice(cut);
      scrollA.append(quill, todo);
    }
  }

  function showEntries(names) {
    entries.textContent = "";
    names.forEach((name) => {
      const chip = document.createElement("span");
      chip.textContent = name;
      entries.appendChild(chip);
    });
  }

  async function ask(question) {
    const asked = (question || "").trim() || FIRST.q;
    const found = ANSWERS.find((item) => item.match.test(asked));
    const text = found ? found.a : UNKNOWN;
    hush();
    const mine = turn;
    scrollQ.textContent = asked;
    showEntries([]);
    scroll.classList.add("speaking");
    scrollStatus.textContent = "George is answering";

    let shown = 0;
    const show = (n) => { if (n > shown) { shown = n; written(text, shown); } };
    written(text, 0);
    const total = spokenFor(text);
    const started = Date.now();
    const clock = setInterval(() => {
      if (mine !== turn) { clearInterval(clock); return; }
      show(Math.floor(text.length * Math.min(1, (Date.now() - started) / total)));
    }, 90);
    if (calm) show(text.length);

    await say(text, (at) => show(at + 6));
    clearInterval(clock);
    if (mine !== turn) return;
    written(text, text.length);
    scroll.classList.remove("speaking");
    scrollStatus.textContent = found ? "George has answered" : "Try one of the questions below";
    showEntries(found ? found.entries : []);
  }

  const chips = $("try-chips");
  ANSWERS.forEach((item) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.textContent = item.q;
    chip.addEventListener("click", () => { tryInput.value = item.q; ask(item.q); });
    chips.appendChild(chip);
  });
  written(FIRST.a, FIRST.a.length);
  showEntries(FIRST.entries);

  $("try-ask").addEventListener("submit", (e) => { e.preventDefault(); ask(tryInput.value); });
  $("hero-ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const question = $("hero-q").value;
    tryInput.value = question;
    $("try").scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
    ask(question);
  });

  // Saying it: only where the browser can listen. If it turns out it cannot, the button goes away.
  const Listen = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = $("try-mic");
  if (Listen) {
    mic.hidden = false;
    let listening = null;
    mic.addEventListener("click", () => {
      if (listening) { listening.stop(); return; }
      hush();
      const ear = new Listen();
      ear.lang = "en-GB";
      ear.interimResults = false;
      ear.onresult = (e) => {
        const heard = e.results[0][0].transcript;
        tryInput.value = heard;
        ask(heard);
      };
      ear.onerror = (e) => {
        if (e.error !== "no-speech" && e.error !== "aborted") {
          mic.hidden = true;
          $("try-note").textContent = "This browser cannot listen on this page, so type your question. In the game, you just hold a key and talk.";
        }
      };
      ear.onend = () => { listening = null; mic.classList.remove("on"); };
      listening = ear;
      mic.classList.add("on");
      try { ear.start(); } catch (err) { listening = null; mic.classList.remove("on"); }
    });
  }

  window.addEventListener("pagehide", hush);
})();
