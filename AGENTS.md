# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Setup, commands and how it works: `README.md`. Tests and lint: `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check src tests tools`.
- Never commit Firaxis or mod text, game files, the built index or the key. `tools/repo_guard.py` enforces this (pre-commit hook via `git config core.hooksPath .githooks`, and `tests/test_repo_guard.py`). Test fixtures are invented and live only in `tests/conftest.py`.
- Game data comes from the captain's PC at run time: `DebugGameplay.sqlite` (rules of the last loaded game) plus the `Modding.log` text replay. Page stats are a port of the game's `CivilopediaPage_*.lua` (Gathering Storm versions); read those files in the game install before changing `src/civspeak/pedia.py`.
- The API key is read only from `~/.config/civspeak/anthropic-key`; never print or log it. Real API calls count against the $10/month ledger in `~/.local/share/civspeak/spend.jsonl`; tests use a fake client.
- Shared 10GB machine: build the index niced (`civspeak build` does this), never search `/mnt/c` recursively (`paths.py` uses fixed paths and shallow globs only).

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
