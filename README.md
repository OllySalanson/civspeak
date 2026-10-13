# civspeak

Ask the Civilopedia out loud: a voice Civilopedia for Civilization VI. Ask what something means,
what it does or where it has to be built, and get a short answer plus the full Civilopedia entries
on your second screen.

Everything comes from **your own installed game**: the rules of the last game you loaded
(Gathering Storm, every DLC and your mods such as Better Balanced Game) and the exact text that
game showed. Nothing from Firaxis or any mod is stored in this repository.

This is the first part of the plan: the Civilopedia index, search, and a typed answer page.
Speaking and the hold-to-talk key come next.

## How to use it

All commands run in WSL from this folder.

1. **Set up once.**

   ```sh
   ./scripts/setup.sh
   ```

2. **Load a game in Civ VI once.** Civ writes its rules cache and `Modding.log` whenever a game
   loads; civspeak reads those. Load a new game (or one with different mods) and civspeak
   rebuilds its index on its own the next time you ask.

3. **Ask from the terminal** (no API key needed; prints the matching entries):

   ```sh
   .venv/bin/civspeak ask "where can I build a campus"
   .venv/bin/civspeak ask --full "what does a ziggurat do"
   ```

4. **Open the answer page** on your second screen:

   ```sh
   .venv/bin/civspeak serve --open
   ```

   This opens `http://localhost:8765` as a Brave app window; drag it to the second screen. Type a
   question and press Enter. You get a one to three sentence answer written by Claude Opus 5.5 from
   the matched entries only, with the entry cards underneath (the ones the answer used are marked).
   Press `/` to jump back to the question box. Stop the server with Ctrl+C.

5. **Check things at any time:**

   ```sh
   .venv/bin/civspeak status
   ```

   shows the game it was built from, whether the API key is present and this month's spend.

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
| The built index (your game's text) | `~/.local/share/civspeak/index.sqlite` |
| Spend ledger | `~/.local/share/civspeak/spend.jsonl` |
| API key | `~/.config/civspeak/anthropic-key` |
| Game install | found automatically under `C:\Program Files (x86)\Steam`; override with `CIVSPEAK_GAME_DIR` |
| Game cache and logs | `%LOCALAPPDATA%\Firaxis Games\Sid Meier's Civilization VI`; override with `CIVSPEAK_USER_DIR` |

## How it works

- **Rules:** `Cache/DebugGameplay.sqlite`, which Civ writes when a game loads, holds the exact rule
  set of that game. Civilopedia pages are built from it the way the in-game Civilopedia does
  (its page queries, chapters and the stats box of each page type: placement, costs, adjacency,
  unique-to and so on).
- **Text:** `Logs/Modding.log` lists every text file the game loaded for that game, in order,
  including mods. civspeak replays that list (base game, DLC, Steam Workshop and local mods) to
  rebuild the English text, then fills the game's placeholders and strips its markup.
- **Search:** page names and the Civilopedia's own search terms are matched exactly, fuzzily
  ("zigurat") and by sound ("hanza" finds Hansa), combined with full-text search. Placement
  questions also bring in the general rules page (for example the Districts concept page).
- **Answer:** the top entries go to Claude Opus 5.5 (low effort, prompt caching on the fixed
  instructions, server-side refusal fallback) with instructions to answer only from them.

## Development

```sh
.venv/bin/python -m pytest -q          # tests use an invented mini game, never real game files
.venv/bin/ruff check src tests tools && .venv/bin/ruff format --check src tests tools
```

`tools/repo_guard.py` runs as the pre-commit hook (and as a test): it refuses game file types, the
index, the key, anything that looks like an API key, game-style text rows outside the test fixture,
and any sentence from your built Civilopedia.
