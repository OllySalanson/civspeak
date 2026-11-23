# Developing Civspeak

How Civspeak is built, run from source and tested. To just use it, see the [README](../README.md).

## Running it from WSL

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
written by Claude from the matched entries only (the model is chosen in Settings, below), starting as soon as the first sentence
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

## The Windows app from source

The same Civspeak as one Windows program, with no WSL and no Brave: the answer page is its own
window and everything else runs inside it. Players install it with `Civspeak-Setup.exe` (see
[Building the Windows installer](#building-the-windows-installer)); from source it runs from a Python
3.10 or newer for Windows, in a Command Prompt in this folder:

```bat
py -m venv %LOCALAPPDATA%\Civspeak\venv
%LOCALAPPDATA%\Civspeak\venv\Scripts\pip install .[voice,app]
%LOCALAPPDATA%\Civspeak\venv\Scripts\pythonw -m civspeak app
```

- The star appears by the clock and the answer page opens filling your second screen (never over
  the game: if Civ has the focus it waits until you leave it). Hold `` ` `` in Civ to ask, as above.
- **The app listens and speaks by itself.** It records your question from the PC's own microphone
  and George answers out of its speakers, so holding `` ` `` in the game works with the window
  closed or on a PC with one screen. The page shows what it heard and the answer whenever it is
  open, and its microphone button, `` ` ``, Esc and **Voice on / Voice off** work the same
  microphone and speakers. Nothing asks for permission to use the microphone; if Windows has
  desktop apps' microphone access turned off, the page says where to turn it on.
- **No microphone?** `` ` `` in the game gives a Windows "ding" and the page, when you next open it,
  says none was found. Plug one in and ask again: no restart needed.
- **Closing the window only hides it.** Holding `` ` `` in the game still works and George still
  answers. Click the star and choose **Open the answer page**, or start Civspeak again, to bring
  it back.
- **Updates:** once a day the packaged app asks GitHub for Civspeak's latest published release.
  When there is a newer one, the star says so and its menu gets **Install Civspeak x.y.z**, as
  does Settings. Installing it downloads `Civspeak-Setup.exe`, checks it against the release's
  `SHA256SUMS.txt` (a file that does not match is never run), starts it silently and turns
  Civspeak off; Setup replaces the program, keeps your settings, key and George's voice, and starts
  the new Civspeak. Running from source never updates itself.
- **Start when I log in** (in the star's menu) has Windows start the app at login; **Turn off
  Civspeak** stops it, page and all.
- Only one Civspeak runs at a time, because both want `` ` `` and the same page: while the WSL
  helper runs, starting the app just shows the helper's page. Turn the helper off first
  (`.venv/bin/civspeak helper uninstall` in WSL). Ticking **Start when I log in** in the app
  replaces the helper at login.
- **First run: a "Set up Civspeak" screen** shows where it found Civ VI (or a **Choose folder...**
  button when it could not), the voice download and **Start Civspeak when I log in to Windows**. No
  key is needed (see [The answer service](#the-answer-service)); **Advanced** holds the player's
  Civspeak ID and their own key, with the Answers choice and monthly limit once a key is added. If your game is
  not in English it says that Civspeak speaks English and uses the English names. The same screen
  is **Settings** afterwards.
- **First run: George's voice and the speech model download by themselves** (268 MB, once) into
  `%LOCALAPPDATA%\Civspeak\models`, from the `models-v1` assets on Civspeak's GitHub Release. Each
  file's size and SHA-256 are written into Civspeak, so a damaged file is thrown away and never used.
  The download runs at low priority, picks up where it stopped if the connection drops or Civspeak
  is turned off half way, and the page says how far it has got. Typed questions work meanwhile.
  `civspeak models` shows what is in place; `civspeak models fetch` fetches it in a terminal.
  George's voice here is Kokoro's int8 model: a third of the size and memory of the full one, at
  the same speed and, to Whisper's ear, saying the same words.

## Your own API key and the spend cap

Optional: without one, answers come from [the answer service](#the-answer-service).

- **Settings > Advanced** takes the key: paste it and Civspeak checks it with Claude's
  Models API, which costs nothing, before keeping it. It never shows the key again.
- In WSL the key lives in `~/.config/civspeak/anthropic-key` (mode 600, outside the repo). The
  Windows app keeps it in **Windows Credential Manager** ("Civspeak/Claude API key", tied to your
  Windows login), and moves a key file left by an earlier Civspeak in there. Any
  `ANTHROPIC_API_KEY` in the environment is ignored, so a work key is never used by accident. The
  key is never printed or logged.
- With no key of your own and no answer service (`CIVSPEAK_ANSWER_URL=off`), the page says so plainly
  and still shows the Civilopedia entries.
- Spending is capped at **$10 a month** in WSL and **$5 a month** in the Windows app, unless you
  choose another limit in Settings. It is tracked locally in `spend.jsonl` in the data folder. Each
  question reserves its worst-case cost before calling Claude, so the cap cannot be exceeded. When
  the cap is reached the page says "Monthly limit reached" and keeps showing entries. A typical
  answer costs about 2 cents on Best and well under a tenth of a cent on Quick.
  (`CIVSPEAK_MONTHLY_CAP_USD` wins over Settings.)

## The answer service

Players need no key: the app sends each question, with the Civilopedia entries it found, to Civspeak's
answer service (`src/civspeak/answer_service.py`), which answers with Civspeak's own key on Google
Cloud Run (project `pixelreel-515501`, region `europe-west1`, service `civspeak-answers`, at
`online.DEFAULT_URL`). It runs the same `Answerer`, instructions and Quick model as the app, so it is
no general-purpose proxy, and it streams the answer back as JSON lines. A player who adds their own key
under **Settings > Advanced** is answered with it directly instead, and only then do the Answers choice
and the monthly spending limit show.

- **Limits**, counted in Firestore in one transaction before every answer: questions a day per
  install (`CIVSPEAK_DAILY_QUESTIONS`), questions a day per internet address
  (`CIVSPEAK_IP_DAILY_QUESTIONS`, so made-up IDs do not get round the first), and a hard monthly cap
  in dollars for everyone (`CIVSPEAK_MONTHLY_CAP_USD`), set below the key's own spend limit in the
  Claude Console. Each answer reserves its worst case first and settles its real cost after, so the
  cap cannot be overshot. **The numbers are kept only in the service's settings on Cloud Run, never
  in this repository** (`service/deploy.sh limits`); without them the service answers nothing.
  Days and months are UTC. Daily counters expire after 3 days (a Firestore TTL policy).
- **The install ID** (`CS-XXXX-XXXX-XXXX`, random, made on first run, kept in `settings.json`) is all
  that identifies a player. When a limit is reached the app shows a pop-up with the ID, a Copy button
  and the address to email for a higher limit. Raise one, with no code change or rebuild:

  ```sh
  service/deploy.sh raise CS-XXXX-XXXX-XXXX <questions a day>
  ```

- **Privacy:** nothing a player asked is stored or logged: only counts, the install ID, a hash of the
  address with the day, and dollars.
- **Deploying** (from the repository root; `gcloud` signed in to the project):

  ```sh
  service/deploy.sh setup    # once: Firestore and its TTL policies, the service account, the image repository
  service/deploy.sh key      # the key from ~/.config/civspeak/answer-service-key, as a Secret Manager secret
  service/deploy.sh limits <per install a day> <per address a day> <dollars a month>   # once, or to change them
  service/deploy.sh          # build (Cloud Build, service/Dockerfile) and deploy; the limits and raised IDs are kept
  ```

- **Cost:** on 22 Nov 2026 an answer cost $0.00006 to $0.00015 on the live service (Haiku 5.5,
  about 2,700 input and 80 output tokens). Cloud Run scales
  to zero (no charge while idle); a sleeping service takes a few seconds to start, so the app wakes
  it (`/healthz`) when it starts and when the talk key is pressed.
- **Tests:** `tests/test_answer_service.py` runs the service with a fake Claude and counters in
  memory, and the app's client against a fake service. `CIVSPEAK_ANSWER_URL` points the app at
  another service (`off` for none); tests point it at a closed port, so they never reach the real one.

## Where things live

| What | Where |
|---|---|
| The built index (your game's text) | `~/.local/share/civspeak/index-<n>.sqlite` (each rebuild is the next number; old ones are removed) |
| Spend ledger | `~/.local/share/civspeak/spend.jsonl` |
| Answer timings | `~/.local/share/civspeak/latency.jsonl` (the last 500 to 1,000 questions) |
| API key | `~/.config/civspeak/anthropic-key` |
| Game install | found automatically: every Steam library on any drive (from Steam's `libraryfolders.vdf`), or the Epic Games launcher's install; or the folder chosen in Settings; override with `CIVSPEAK_GAME_DIR` |
| Settings | `settings.json` in the config folder: the monthly limit, a Civ VI folder chosen by hand, and whether the first-run screen is done |
| George's voice (Kokoro model) | `~/.local/share/pixel-explainer/kokoro-v1.0.onnx` and `voices-v1.0.bin`; override with `CIVSPEAK_KOKORO_DIR` |
| Speech recognition model | `~/.cache/huggingface` (Whisper `base.en`); `CIVSPEAK_WHISPER_MODEL=small.en` tries the bigger one |
| Game cache and logs | `%LOCALAPPDATA%\Firaxis Games\Sid Meier's Civilization VI` (or its `(Epic)` twin, whichever game was loaded last); override with `CIVSPEAK_USER_DIR` |
| Mods installed by hand | `Documents\My Games\Sid Meier's Civilization VI\Mods`, wherever Windows (or OneDrive) keeps Documents; override with `CIVSPEAK_MODS_DIR` |
| The Windows helper | `%LOCALAPPDATA%\Civspeak`: the helper, its `config.json`, the page's own Brave profile, `server.log` and `helper.log` |
| The Windows app | `%LOCALAPPDATA%\Civspeak`: `app.log` (what went wrong, if anything), the page window's `webview` folder (it remembers Voice on or off) and `civspeak.ico` |

Running under Windows' own Python instead of WSL (the coming Windows app), Civspeak keeps the index,
spend ledger, timings and key in `%LOCALAPPDATA%\Civspeak`, and downloads George's voice
(`kokoro-v1.0.int8.onnx`, `voices-v1.0.bin`) and the speech model (`whisper-base.en`) into its
`models` folder (`CIVSPEAK_MODELS_DIR` moves it, `CIVSPEAK_MODELS_URL` fetches from a mirror; the
variables above still override each model). The game is found the same way, with Steam's folder read from the registry and
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
- **Answer:** the top entries go to Claude (low effort, prompt caching on the fixed instructions)
  with instructions to answer only from them. **Settings > Answers** picks the model:
  **Quick** (Claude Haiku 5.5, the Windows app's default), **Balanced** (Claude Sonnet 5.5) or
  **Best** (Claude Opus 5.5, the WSL route's default); `CIVSPEAK_ANSWERS=quick|balanced|best`
  overrides it. Balanced and Best hand a declined question to another model (server-side refusal
  fallback); Haiku has none, so a question it declines gets no answer.
  `tools/answer_check.py` asks 30 questions of each model and writes the answers side by side
  (real calls, under $1 in all). On 22 Nov 2026 an answer cost about 0.05 cents on Quick, 0.7 cents
  on Balanced and 1.5 cents on Best; Best and Balanced were right every time or nearly, and Quick
  on about 26 of 30.
- **Listening:** the page records the microphone as 16 kHz audio and sends it to the local server,
  where Whisper base.en turns it into text on the CPU. (The Windows app records it itself instead,
  see below.) Whisper is given a hint list of about 90
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
- **The Windows app** (`src/civspeak/app.py`) is one process: the page's server runs on a thread,
  the star, its menu and `` ` `` (the same code as the helper) on another, and the page in a
  pywebview window on WebView2, which Windows 10 and 11 already have. The whole process runs below
  normal priority, except the thread that takes `` ` ``, so the key is answered at once even while
  the game keeps the PC busy.
- **The app's own microphone and speakers** (`src/civspeak/conversation.py` and `audio.py`, through
  sounddevice, which carries PortAudio): `` ` `` or the page's microphone button opens the default
  microphone for that one question (at 16 kHz, Whisper's rate, when it allows), with the page's
  rules for when you have finished (hold and let go, or tap and stop talking). Without the
  browser's automatic gain, "talking" is anything well above the room's own level, so a quiet
  microphone still works. Each of George's sentences is written to the speakers as soon as it is
  voiced, 50 ms at a time, so Esc or a new question stops him at once. Every open page is told
  what happens on its event stream and shows it; the page itself never records or plays sound,
  and WebView2 grants it no permission at all.
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
.venv/bin/ruff check src tests tools packaging && .venv/bin/ruff format --check src tests tools packaging
```

### Building the Windows installer

`.github/workflows/build.yml` builds `Civspeak-Setup.exe` on GitHub's Windows machines, never on
this PC: on every pull request that touches the app, and on a version tag (`v1.2.3`, which must match
`civspeak.__version__`), where it also drafts a GitHub Release for a person to publish. It installs
the pinned packages in `packaging/requirements-windows.txt` (PyAV left out: `packaging/stubs/av`
stands in for it), builds the one-folder `Civspeak.exe` with `packaging/civspeak.spec`, runs
`Civspeak.exe --selftest` on it, makes the installer with `packaging/civspeak.iss` (Inno Setup,
per user, no administrator prompt), installs it silently, runs the self-test again, installs it
again the way the updater does (`/UPDATE=1`: the settings must survive and Civspeak must start
again by itself) and uninstalls. A Release the updater offers needs both `Civspeak-Setup.exe` and
`SHA256SUMS.txt`; the workflow attaches both.
`civspeak selftest` runs the same checks from source.

`tools/voice_check.py` tests the whole spoken path without a person: Kokoro says each question in
a different voice and it goes through the microphone's endpoints of a running server, with timings.
Each question is a real Claude call (about 2 cents on Best):

```sh
.venv/bin/python tools/voice_check.py "Where can I build a Campus?" "Tell me about Kongo."
.venv/bin/python tools/voice_check.py --wav my-question.wav   # a real recording
```

### The website

The website is the plain files in `site/`: one page (`index.html`), one stylesheet, one script, the
fonts and the pictures. There is no build step. `.github/workflows/pages.yml` publishes the folder
to GitHub Pages (<https://ollysalanson.github.io/civspeak/>) whenever `site/` changes on `main`.
To look at it here:

```sh
python3 -m http.server 5197 -d site      # then open http://localhost:5197
```

- **The hero's pictures** are `site/img/ages/1-ancient.webp` to `8-information.webp`, one painting
  for each age, shown one at a time; two arrows on the picture step through them. Each is 3200 by
  1000 (saved at 2560 by 800) with what matters in the middle and the top two fifths plain night
  sky, because the heading sits over it. To change the art, replace the files; their names and
  descriptions are in the `AGES` list in `site/js/site.js`. `docs/site-art/previous/` keeps the
  last version of any picture that was changed, to go back to.
- **The painted look below the hero** is in `site/img/paint/`: `brush.webp` is a grey brush
  texture that tiles and is blended over each section's plain colour, `edge.svg` is the ragged
  edge where one section's paint is brushed over the one above, `key`, `ask` and `answer` are the
  small paintings in How it works, and `band.webp` is the valley at night under Download. The
  section colours are the variables at the top of `site/css/site.css`.
- **No sound files.** `tools/repo_guard.py` refuses recordings, so the answer in Ask it yourself
  is read by the browser's own speech. Nothing plays until Ask is pressed.
- **Ask it yourself** answers from the short `ANSWERS` list in `site/js/site.js`, written for the
  page. A web page has no game to read, so it cannot look anything else up.
- `tests/test_site.py` holds the site's rules: every linked file exists, Download fetches the
  latest installer, and the words never name who writes the answers, any limit on asking, or the
  game's makers.

`tools/repo_guard.py` runs as the pre-commit hook (and as a test): it refuses game file types, the
index, the key, anything that looks like an API key, game-style text rows outside the test fixture,
any sentence from your built Civilopedia, and voice models or recordings.
