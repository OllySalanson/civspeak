// Civ Speak's website: the eight ages in the hero, and the Ask it yourself demo.
// Nothing here makes a sound until something is pressed. There are no sound files: voices come from
// the browser's own speech, and the background sounds are made here with Web Audio.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------------------------------------------------------------- voices
  const synth = window.speechSynthesis || null;
  let voices = [];
  const loadVoices = () => { voices = synth ? synth.getVoices() : []; };
  if (synth) {
    loadVoices();
    if (synth.addEventListener) synth.addEventListener("voiceschanged", loadVoices);
  }

  // British voices if the device has any, English otherwise. "george" asks for the nearest thing
  // to George; a number picks one of the others so the ages do not all sound alike.
  function pickVoice(want) {
    const english = voices.filter((v) => /^en([-_]|$)/i.test(v.lang));
    const british = english.filter((v) => /^en[-_]GB/i.test(v.lang));
    const pool = british.length ? british : english;
    if (!pool.length) return null;
    if (want === "george") {
      return pool.find((v) => /george/i.test(v.name))
        || pool.find((v) => /(ryan|daniel|arthur|oliver|thomas|brian|\bmale)/i.test(v.name) && !/female/i.test(v.name))
        || pool[0];
    }
    return pool[want % pool.length];
  }

  const spokenFor = (text, rate) => text.length * 68 / (rate || 1) + 500;
  let turn = 0; // goes up whenever something new starts, so whatever was talking knows to stop

  function say(text, opts) {
    const o = opts || {};
    if (!synth || !window.SpeechSynthesisUtterance) return wait(spokenFor(text, o.rate));
    return new Promise((done) => {
      const u = new SpeechSynthesisUtterance(text);
      const voice = pickVoice(o.voice === undefined ? "george" : o.voice);
      if (voice) { u.voice = voice; u.lang = voice.lang; } else { u.lang = "en-GB"; }
      u.rate = o.rate || 1;
      u.pitch = o.pitch || 1;
      let over = false;
      const finish = () => { if (!over) { over = true; done(); } };
      u.onboundary = (e) => { if (o.onword) o.onword(e.charIndex); };
      u.onend = finish;
      u.onerror = finish;
      setTimeout(finish, spokenFor(text, o.rate) * 2 + 4000); // some browsers never say they have finished
      synth.speak(u);
    });
  }

  // ---------------------------------------------------------------- background sounds
  let ctx = null;
  let noiseBuffer = null;
  function audio() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    if (!ctx) {
      ctx = new Ctx();
      noiseBuffer = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate);
      const data = noiseBuffer.getChannelData(0);
      for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
    }
    if (ctx.state === "suspended") ctx.resume();
    return ctx;
  }

  const MORSE = { A: ".-", D: "-..", E: ".", G: "--.", H: "....", N: "-.", O: "---", R: ".-.", S: "...", T: "-", W: ".--", Y: "-.--" };

  // Starts one of the background sounds and hands back the way to stop it.
  function ambience(kind) {
    const ac = kind && kind !== "none" ? audio() : null;
    if (!ac) return () => {};
    const out = ac.createGain();
    out.gain.value = 0;
    out.connect(ac.destination);
    out.gain.linearRampToValueAtTime(1, ac.currentTime + 0.3);
    const running = [];
    const timers = [];

    const hiss = (type, freq, q, level) => {
      const src = ac.createBufferSource();
      src.buffer = noiseBuffer;
      src.loop = true;
      const filter = ac.createBiquadFilter();
      filter.type = type; filter.frequency.value = freq; filter.Q.value = q;
      const gain = ac.createGain();
      gain.gain.value = level;
      src.connect(filter); filter.connect(gain); gain.connect(out);
      src.start();
      running.push(src);
    };
    const burst = (type, freq, level, length, at) => {
      const t = ac.currentTime + (at || 0);
      const src = ac.createBufferSource();
      src.buffer = noiseBuffer;
      const filter = ac.createBiquadFilter();
      filter.type = type; filter.frequency.value = freq; filter.Q.value = 1.2;
      const gain = ac.createGain();
      gain.gain.setValueAtTime(level, t);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + length);
      src.connect(filter); filter.connect(gain); gain.connect(out);
      src.start(t, Math.random() * 1.5, length + 0.05);
    };
    const tone = (freq, at, length, level, shape, slideTo) => {
      const t = ac.currentTime + at;
      const osc = ac.createOscillator();
      osc.type = shape || "sine";
      osc.frequency.setValueAtTime(freq, t);
      if (slideTo) osc.frequency.linearRampToValueAtTime(slideTo, t + length);
      const gain = ac.createGain();
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(level, t + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + length);
      osc.connect(gain); gain.connect(out);
      osc.start(t); osc.stop(t + length + 0.05);
    };
    const every = (ms, fn) => timers.push(setInterval(fn, ms));

    if (kind === "fire") {
      hiss("lowpass", 700, 0.5, 0.11);
      every(110, () => { if (Math.random() < 0.5) burst("highpass", 2400 + Math.random() * 1500, 0.1, 0.03); });
    } else if (kind === "agora") {
      hiss("bandpass", 420, 0.6, 0.06);
      [392, 440, 523, 587, 523, 440, 392].forEach((f, i) => tone(f, 0.3 + i * 0.55, 0.6, 0.05, "triangle"));
    } else if (kind === "quill") {
      tone(660, 0.15, 2.4, 0.045); tone(990, 0.15, 2.0, 0.025);
      every(270, () => burst("bandpass", 3600 + Math.random() * 900, 0.05, 0.13));
    } else if (kind === "bell") {
      hiss("bandpass", 350, 0.6, 0.05);
      [0, 0.45, 0.9].forEach((at) => { tone(1320, at, 0.55, 0.06); tone(1980, at, 0.45, 0.035); tone(2640, at, 0.3, 0.02); });
    } else if (kind === "telegraph") {
      const dit = 0.058;
      let at = 0.1;
      for (const ch of "WHAT DOES A GRANARY DO") {
        if (ch === " ") { at += dit * 4; continue; }
        for (const mark of MORSE[ch]) {
          const length = mark === "." ? dit : dit * 3;
          tone(640, at, length, 0.06, "square");
          at += length + dit;
        }
        at += dit * 2;
      }
    } else if (kind === "radio") {
      hiss("highpass", 1800, 0.5, 0.035);
      tone(900, 0, 0.5, 0.02, "sine", 1400);
      every(160, () => { if (Math.random() < 0.35) burst("highpass", 3000, 0.07, 0.02); });
    } else if (kind === "control") {
      hiss("bandpass", 2200, 0.8, 0.03);
      tone(2525, 0, 0.25, 0.045);
    }

    return () => {
      timers.forEach(clearInterval);
      out.gain.cancelScheduledValues(ac.currentTime);
      out.gain.setTargetAtTime(0, ac.currentTime, 0.15);
      setTimeout(() => {
        running.forEach((src) => { try { src.stop(); } catch (e) { /* already stopped */ } });
        out.disconnect();
      }, 800);
    };
  }

  let stopAmbience = () => {};
  function hush() {
    turn++;
    if (synth) synth.cancel();
    stopAmbience();
    stopAmbience = () => {};
  }

  // ---------------------------------------------------------------- the eight ages
  const ANSWER = "It gives the city 1 Food and 2 Housing. You need Pottery to build it.";
  const AGES = [
    { n: "I", era: "Ancient", place: "By the fire", file: "1-ancient", sound: "fire", voice: 1, rate: 0.95, pitch: 1.25,
      line: "Old one, tell us. What is a granary for?",
      alt: "A painting of a valley at dawn. An old storyteller sits on a rock by a fire with two children listening. Behind are clay granaries, a palm tree, and a stepped temple across the river." },
    { n: "II", era: "Classical", place: "In the agora", file: "2-classical", sound: "agora", voice: 2, rate: 1, pitch: 1.05,
      line: "Tell me, philosopher: what is the purpose of a granary?",
      alt: "A painting of the valley in the morning. A bearded philosopher in a white robe raises one arm while a young man listens from a marble block. Behind are a columned porch, an olive tree, a temple on a rock and an aqueduct." },
    { n: "III", era: "Medieval", place: "In the scriptorium", file: "3-medieval", sound: "quill", voice: 3, rate: 0.92, pitch: 0.9,
      line: "Brother, read me the page on granaries.",
      alt: "A painting of the valley by day. A monk in a brown habit reads aloud from a book on a lectern by a tall candle, while another monk listens on a bench. Behind are a stone chapel with a coloured window, an oak, a castle and a windmill." },
    { n: "IV", era: "Renaissance", place: "In the town square", file: "4-renaissance", sound: "bell", voice: 4, rate: 1.05, pitch: 1.1,
      line: "Crier! What news of granaries?",
      alt: "A painting of the valley in golden light. A town crier in a red coat stands on a box ringing a bell and holding a long scroll, while a woman and a child listen. Behind are a timbered house, a pennant, a great dome, a bell tower and a ship." },
    { n: "V", era: "Industrial", place: "At the telegraph office", file: "5-industrial", sound: "telegraph", voice: 5, rate: 1.1, pitch: 0.8,
      line: "WHAT DOES A GRANARY DO STOP", spoken: "What does a granary do. Stop.", wire: true,
      alt: "A painting of the valley at dusk. A clerk in a bowler hat taps a telegraph key at a desk under a green lamp. Behind are a wooden telegraph hut, a pole with wires, a works with two smoking chimneys, and a train on a viaduct." },
    { n: "VI", era: "Modern", place: "On the wireless", file: "6-modern", sound: "radio", voice: 6, rate: 1.02, pitch: 1,
      line: "A listener writes in to ask: what does a granary do?",
      alt: "A painting of the valley at sunset. A radio announcer in a suit reads from a page into a microphone, while a listener sits in an armchair by a glowing wireless set. Behind are a small station with an On Air sign, a city of lit towers, a bridge and an airship." },
    { n: "VII", era: "Atomic", place: "At mission control", file: "7-atomic", sound: "control", voice: 7, rate: 1.08, pitch: 0.95,
      line: "Control, come in. What does a granary do? Over.",
      alt: "A painting of the valley at twilight. A controller in a headset sits at a console with two green screens. Behind are a concrete blockhouse, a red and white mast, a rocket on its pad and a large dish." },
    { n: "VIII", era: "Information", place: "At your desk", file: "8-information", sound: "none", voice: 8, rate: 1, pitch: 1.1,
      line: "What does a Granary do?",
      alt: "A painting of the valley at night. Someone in a headset sits at a desk, talking to a screen that shows a gold star and a sound wave. A cat watches. Behind are a house with a lit glass wall and a city of glass towers." },
  ];

  const art = $("art");
  const ageLine = $("age-line");
  const ageName = $("age-name");
  const playButton = $("age-play");
  const pictures = [$("art-first")];
  let age = 0;
  let playing = false;

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

  function showLine(label, text, wire) {
    ageLine.textContent = "";
    const small = document.createElement("small");
    small.textContent = label;
    ageLine.append(small, text);
    ageLine.classList.toggle("wire", !!wire);
  }

  function setPlaying(on) {
    playing = on;
    playButton.querySelector("span").textContent = on ? "Stop" : "Hear the question";
    playButton.querySelector("path").setAttribute("d", on ? "M6 5h4v14H6zM14 5h4v14h-4z" : "M7 4l13 8-13 8z");
  }

  function showAge(i) {
    hush();
    setPlaying(false);
    age = (i + AGES.length) % AGES.length;
    const a = AGES[age];
    const next = picture(age);
    pictures.forEach((img) => { if (img) img.classList.toggle("on", img === next); });
    ageName.querySelector("b").textContent = a.n + " · " + a.era;
    ageName.querySelector("span").textContent = a.place;
    showLine("Asked " + a.place.charAt(0).toLowerCase() + a.place.slice(1), "“" + a.line + "”", a.wire);
    // have the neighbours ready, so the arrows feel quick
    setTimeout(() => { picture((age + 1) % AGES.length); picture((age + AGES.length - 1) % AGES.length); }, 600);
  }

  async function playAge() {
    if (playing) { hush(); setPlaying(false); showAge(age); return; }
    hush();
    const mine = turn;
    const a = AGES[age];
    setPlaying(true);
    stopAmbience = ambience(a.sound);
    await wait(a.sound === "none" ? 100 : 700);
    if (mine !== turn) return;
    await say(a.spoken || a.line, { voice: a.voice, rate: a.rate, pitch: a.pitch });
    if (mine !== turn) return;
    if (age === AGES.length - 1) {
      await wait(350);
      if (mine !== turn) return;
      showLine("George answers", "“" + ANSWER + "”");
      await say(ANSWER, { voice: "george", rate: 0.98, pitch: 0.9 });
      if (mine !== turn) return;
    }
    await wait(500);
    if (mine !== turn) return;
    stopAmbience();
    stopAmbience = () => {};
    setPlaying(false);
  }

  $("age-prev").addEventListener("click", () => showAge(age - 1));
  $("age-next").addEventListener("click", () => showAge(age + 1));
  playButton.addEventListener("click", playAge);
  $("ages").addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { showAge(age - 1); e.preventDefault(); }
    if (e.key === "ArrowRight") { showAge(age + 1); e.preventDefault(); }
  });
  showAge(0);

  // ---------------------------------------------------------------- Ask it yourself
  // A handful of answers, written for this page. The app itself reads the Civilopedia from the
  // player's own game, so it can answer anything in it; a web page has no game to read.
  const ANSWERS = [
    { q: "What does a Granary do?", match: /granar/i, entries: ["Granary", "Pottery"], a: ANSWER },
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
    const asked = (question || "").trim() || ANSWERS[1].q;
    const found = ANSWERS.find((item) => item.match.test(asked));
    const text = found ? found.a : UNKNOWN;
    hush();
    setPlaying(false);
    const mine = turn;
    scrollQ.textContent = asked;
    showEntries([]);
    scroll.classList.add("speaking");
    scrollStatus.textContent = "George is answering";

    let shown = 0;
    const show = (n) => { if (n > shown) { shown = n; written(text, shown); } };
    written(text, 0);
    const total = spokenFor(text, 0.98);
    const started = Date.now();
    const clock = setInterval(() => {
      if (mine !== turn) { clearInterval(clock); return; }
      show(Math.floor(text.length * Math.min(1, (Date.now() - started) / total)));
    }, 90);
    if (calm) show(text.length);

    await say(text, { voice: "george", rate: 0.98, pitch: 0.9, onword: (at) => show(at + 6) });
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
  written(ANSWERS[1].a, ANSWERS[1].a.length);
  showEntries(ANSWERS[1].entries);

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
