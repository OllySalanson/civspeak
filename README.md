# civspeak

Ask the Civilopedia out loud: a voice Civilopedia for Civilization VI. Ask what something means,
what it does or where it has to be built, and get a short answer plus the full Civilopedia entries
on your second screen.

Everything comes from **your own installed game**: the rules of the last game you loaded
(Gathering Storm, every DLC and your mods such as Better Balanced Game) and the exact text that
game showed. Nothing from Firaxis or any mod is stored in this repository.

## How to use it

### Set it up once

This is the only part that needs a terminal (WSL, in this folder), and only once.

1. **Install.**

   ```sh
   ./scripts/setup.sh
   ```

   This installs everything: the voice (Kokoro) and speech recognition (Whisper base.en), both
   running on this PC so no audio leaves it, and the **Civspeak helper for Windows**. The helper
   puts a **Civspeak** icon (a gold star) in the Start menu and on the desktop, sits by the clock,
   and starts by itself every time you log in. Nothing needs to be clicked or allowed on Windows
   to install it: no administrator rights, no security prompts.

2. **Load a game in Civ VI once.** Civ writes its rules cache and `Modding.log` whenever a game
   loads; Civspeak reads those. Load a new game (or one with different mods) and it rebuilds its
   index on its own the next time you ask.

3. **Allow the microphone, once.** The answer page opens filling your second screen a few seconds
   after the install. Click the round microphone button on it; Brave asks whether
   `localhost:8765` may use your microphone: click **Allow**. (It stops listening by itself.)

   That is the only click Windows or Brave ever needs. If the page instead says the microphone is
   blocked, turn on Windows **Settings > Privacy & security > Microphone > Let desktop apps access
   your microphone**.

4. **Optional: keep the star by the clock.** Windows 11 puts new icons under the **^** arrow next
   to the clock. To keep Civspeak's star in view, drag it from there onto the taskbar.

### Every day, no terminal

- **Log in.** Civspeak starts by itself and the answer page opens on your second screen.
- **In Civ, hold `` ` ``** (the key under Esc), ask your question, and let go. For example "where
  can I build a Mbanza?". George reads out a short answer and the Civilopedia entries appear on
  the second screen. A quick tap of `` ` `` instead listens until you stop talking.
  - Civ never sees the key and keeps the focus: you stay in the game. The key is only taken while
    Civ has the focus, so `` ` `` types normally everywhere else.
  - If you hear a Windows "ding" instead, Civspeak is not ready: it is still starting, or its
    page was closed. A closed page comes back as soon as you leave the game (Alt+Tab).
- **Open the page** if you closed it: double-click **Civspeak** on the desktop or in the Start
  menu, or click the star by the clock and choose **Open the answer page**. (If Civspeak was turned
  off, the shortcut starts it again.)
- **Turn it off:** click the star by the clock and choose **Turn off Civspeak**. That stops
  everything (the page closes) until you log in again or open the shortcut.
- **Stop it starting at login:** click the star and untick **Start when I log in** (or turn
  Civspeak off in Task Manager > Startup apps, where it is listed as "Python"). To remove it
  completely, run `.venv/bin/civspeak helper uninstall` in WSL.

### On the answer page

George (Kokoro's `bm_george` voice at 0.92 speed) reads out a one to three sentence answer
written by Claude Opus 5.5 from the matched entries only, starting as soon as the first sentence
is written. The entry cards appear underneath (the ones the answer used are marked), and what it
heard you say is in the question box.

- **Click the microphone** and ask; it stops listening by itself when you stop talking. Or hold
  the microphone button, or hold `` ` `` while the page has the focus, and let go when you're done.
- **Esc** stops George mid-answer (or cancels listening). Asking a new question also stops him.
- **Voice on / Voice off** (top right) turns the spoken answers off and on; the page remembers.
- You can still type a question and press Enter; the answer is spoken too. Press `/` to jump to
  the question box.
- If it mishears a name, the search still tries names that sound alike ("embancer" finds
  Mbanza), and Claude is told the question was spoken. If it still gets it wrong, ask again or
  type it.

### From the terminal (optional)

```sh
.venv/bin/civspeak ask "where can I build a campus"      # the matching entries, no API key needed
.venv/bin/civspeak ask --full "what does a ziggurat do"
.venv/bin/civspeak serve --open    # the page without the helper (opens in your usual Brave); Ctrl+C stops it
.venv/bin/civspeak status          # the game it was built from, the API key, this month's spend, how quick answers are
.venv/bin/civspeak helper status   # whether the Windows helper is running and starts at login
```

### The API key and the spend cap

- The key is read only from `~/.config/civspeak/anthropic-key` (mode 600, outside the repo). Any
  `ANTHROPIC_API_KEY` in the environment is ignored, so a work key is never used by accident. The
  key is never printed or logged.
- Without the key the page says so plainly and still shows the Civilopedia entries.
- Spending is capped at **$10 a month**, tracked locally in `~/.local/share/civspeak/spend.jsonl`.
  Each question reserves its worst-case cost before calling Claude, so the cap cannot be exceeded.
  When the cap is reached the page says "Monthly limit reached" and keeps showing entries. A typical
  answer costs about 2 cents. (`CIVSPEAK_MONTHLY_CAP_USD` changes the cap.)

### Where things live

| What | Where |
|---|---|
| The built index (your game's text) | `~/.local/share/civspeak/index-<n>.sqlite` (each rebuild is the next number; old ones are removed) |
| Spend ledger | `~/.local/share/civspeak/spend.jsonl` |
| Answer timings | `~/.local/share/civspeak/latency.jsonl` (the last 500 to 1,000 questions) |
| API key | `~/.config/civspeak/anthropic-key` |
| Game install | found automatically: every Steam library on any drive (from Steam's `libraryfolders.vdf`), or the Epic Games launcher's install; override with `CIVSPEAK_GAME_DIR` |
| George's voice (Kokoro model) | `~/.local/share/pixel-explainer/kokoro-v1.0.onnx` and `voices-v1.0.bin`; override with `CIVSPEAK_KOKORO_DIR` |
| Speech recognition model | `~/.cache/huggingface` (Whisper `base.en`); `CIVSPEAK_WHISPER_MODEL=small.en` tries the bigger one |
| Game cache and logs | `%LOCALAPPDATA%\Firaxis Games\Sid Meier's Civilization VI` (or its `(Epic)` twin, whichever game was loaded last); override with `CIVSPEAK_USER_DIR` |
| Mods installed by hand | `Documents\My Games\Sid Meier's Civilization VI\Mods`, wherever Windows (or OneDrive) keeps Documents; override with `CIVSPEAK_MODS_DIR` |
| The Windows helper | `%LOCALAPPDATA%\Civspeak`: the helper, its `config.json`, the page's own Brave profile, `server.log` and `helper.log` |

Running under Windows' own Python instead of WSL (the coming Windows app), Civspeak keeps the index,
spend ledger, timings and key in `%LOCALAPPDATA%\Civspeak`, and expects George's voice in its
`models` folder and the speech model in `models\whisper-base.en` (the same environment variables
override each). The game is found the same way, with Steam's folder read from the registry and
Documents from Windows itself.

## How it works

- **Rules:** `Cache/DebugGameplay.sqlite`, which Civ writes when a game loads, holds the exact rule
  set of that game. Civilopedia pages are built from it the way the in-game Civilopedia does
  (its page queries, chapters and the stats box of every page type: placement, costs, adjacency,
  unique-to, upgrades, abilities and so on), including the page types the Better Civilopedia mod
  adds (World Congress, dedications, alliances, unit tables, the adjacency overview).
- **Text:** `Logs/Modding.log` lists every text file the game loaded for that game, in order,
  including mods. civspeak replays that list (base game, DLC, Steam Workshop and local mods) to
  rebuild the English text, then fills the game's placeholders and strips its markup.
- **Search:** page names and the Civilopedia's own search terms are matched exactly, fuzzily
  ("zigurat") and by sound ("hanza" finds Hansa), combined with full-text search. Placement
  questions also bring in the general rules page (for example the Districts concept page).
- **Answer:** the top entries go to Claude Opus 5.5 (low effort, prompt caching on the fixed
  instructions, server-side refusal fallback) with instructions to answer only from them.
- **Listening:** the page records the microphone as 16 kHz audio and sends it to the local server,
  where Whisper base.en turns it into text on the CPU. Whisper is given a hint list of about 90
  names from your own index (civilization-specific districts, buildings and improvements,
  civilizations, leaders, wonders), so it spells "Kongo" and "Mbanza" right.
- **Speaking:** the answer is cut into sentences as Claude writes it (a long first sentence at its
  first comma), each is voiced by Kokoro as soon as it is complete, and the page plays them back to
  back. So George starts while Claude is still writing.
- **Talking in the game:** the Windows helper (`src/civspeak/winhelper.py`, run by Windows' own
  Python, standard library only) watches which window has the focus. While it is Civ VI, it
  takes the `` ` `` key with Windows' `RegisterHotKey`, so the key press goes to the helper and
  never to the game, and the game keeps the focus. Windows reports only the press, so while the
  key is held the helper checks it every 20 ms (`GetAsyncKeyState`) to catch the release. It
  passes "down" and "up" to the server, which hands them to the open page over `/api/talk`; the
  page records exactly as if you held its own microphone button. A low-level keyboard hook could
  see the release directly, but every key you type in the game would pass through it; the hotkey
  touches nothing else. The helper starts the server with `wsl.exe` and stops it by closing its
  input (or, if the helper ever crashes, Windows ends it). The page runs in its own Brave profile
  so it may start the microphone and George's voice without a click on the page, and stays
  fully awake while the game has the focus.
- **Quick first answer:** when the server starts it loads Whisper and Kokoro and runs each once
  (the first run is the slowest), and imports Claude's client library (about a second on its own),
  so the first question is as quick as the rest.
- **Staying light:** Whisper and Kokoro use at most 4 CPU threads each (`CIVSPEAK_THREADS`, 1-8:
  lower it if the game ever stutters while George talks), the server runs at low priority
  (`nice`; below normal priority on Windows), and Kokoro's threads sleep between sentences
  instead of spinning, which measured a third less CPU per sentence at the same speed. Nothing
  runs between questions. After 20 minutes
  unused (`CIVSPEAK_IDLE_MINUTES`, 0 = never) both are unloaded, giving back about 650 MB; pressing
  `` ` ``, the microphone or typing a question loads them again while you are still asking, so the
  next answer is only a moment slower. The Windows helper sleeps until the focused window changes
  or the key is pressed: no CPU while idle (measured 0 ms over 10 s) and about 25 MB of memory.
- **How long answers take:** every question's timings (listening, search, Claude's first words,
  George's first sentence, whether a model had to load first) go to
  `~/.local/share/civspeak/latency.jsonl`: timings only, never what was said. `civspeak status`
  shows the median time to George's first word over the last 20 questions.

## Development

```sh
.venv/bin/python -m pytest -q          # tests use an invented mini game, never real game files
.venv/bin/ruff check src tests tools && .venv/bin/ruff format --check src tests tools
```

`tools/voice_check.py` tests the whole spoken path without a person: Kokoro says each question in
a different voice and it goes through the microphone's endpoints of a running server, with timings.
Each question is a real Claude call (about 2 cents):

```sh
.venv/bin/python tools/voice_check.py "Where can I build a Campus?" "Tell me about Kongo."
.venv/bin/python tools/voice_check.py --wav my-question.wav   # a real recording
```

`tools/repo_guard.py` runs as the pre-commit hook (and as a test): it refuses game file types, the
index, the key, anything that looks like an API key, game-style text rows outside the test fixture,
any sentence from your built Civilopedia, and voice models or recordings.
