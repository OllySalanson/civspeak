# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Setup, commands and how it works: `README.md`. Tests and lint: `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check src tests tools`.
- Never commit Firaxis or mod text, game files, the built index or the key. `tools/repo_guard.py` enforces this (pre-commit hook via `git config core.hooksPath .githooks`, and `tests/test_repo_guard.py`). Test fixtures are invented and live only in `tests/conftest.py`.
- Game data comes from the captain's PC at run time: `DebugGameplay.sqlite` (rules of the last loaded game) plus the `Modding.log` text replay. Page stats are a port of the game's `CivilopediaPage_*.lua` (Gathering Storm versions); read those files in the game install before changing `src/civspeak/pedia.py`.
- The API key is read only from `~/.config/civspeak/anthropic-key`; never print or log it. Real API calls count against the $10/month ledger in `~/.local/share/civspeak/spend.jsonl`; tests use a fake client.
- Voice: Kokoro model files are shared from `~/.local/share/pixel-explainer`, Whisper `base.en` from `~/.cache/huggingface` (`paths.py`); never download at run time or commit models or audio. Tests use fake Kokoro/Whisper (`tests/test_voice.py`); `tools/voice_check.py` exercises the real spoken path against a running server and spends real money.
- Windows helper (`src/civspeak/winhelper.py`): runs under Windows' own Python, so standard library only and Python 3.10+; it must stay importable on Linux (ctypes/winreg only inside functions) because `tests/test_talk.py` tests its logic there. Exercise the Windows half by running a copy with `python.exe` from WSL (interop shares the captain's desktop); check `GetLastInputInfo` first and never take focus, open windows or inject keys while he is using the PC.
- Shared 10GB machine: build the index niced (`civspeak build` does this), never search `/mnt/c` recursively (`paths.py` uses fixed paths and shallow globs only).

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
