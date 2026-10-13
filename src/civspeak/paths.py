"""Where things live: the game install, the game's cache and logs, mods, and civspeak's own files.

Everything is discovered without searching the disk: each location is a fixed, well-known path
(optionally overridden by an environment variable), checked with a single shallow glob at most.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path

GAME_FOLDER = "Sid Meier's Civilization VI"
STEAM_APP_ID = "289070"

DEFAULT_GAME_DIRS = [
    f"/mnt/c/Program Files (x86)/Steam/steamapps/common/{GAME_FOLDER}",
    f"/mnt/c/Program Files/Steam/steamapps/common/{GAME_FOLDER}",
]


class SetupError(RuntimeError):
    """Something civspeak needs on this PC is missing; the message says what to do."""


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(os.environ.get("CIVSPEAK_DATA_DIR") or os.path.join(base, "civspeak"))


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(os.environ.get("CIVSPEAK_CONFIG_DIR") or os.path.join(base, "civspeak"))


def key_file() -> Path:
    return config_dir() / "anthropic-key"


def index_file() -> Path:
    return data_dir() / "index.sqlite"


def spend_file() -> Path:
    return data_dir() / "spend.jsonl"


def _first_existing(candidates: list[str]) -> Path | None:
    for c in candidates:
        if c and os.path.isdir(c):
            return Path(c)
    return None


def _windows_user_globs(suffix: str) -> list[str]:
    # One shallow glob over the Windows user folders; never a recursive search.
    return sorted(glob.glob(f"/mnt/c/Users/*/{suffix}"))


@dataclass
class GamePaths:
    game_dir: Path
    user_dir: Path
    mod_roots: list[Path] = field(default_factory=list)

    @property
    def gameplay_db(self) -> Path:
        return self.user_dir / "Cache" / "DebugGameplay.sqlite"

    @property
    def modding_log(self) -> Path:
        return self.user_dir / "Logs" / "Modding.log"

    @property
    def base_text_dir(self) -> Path:
        return self.game_dir / "Base" / "Assets" / "Text" / "en_US"

    def sources(self) -> list[Path]:
        """Files whose change means the index is out of date."""
        return [self.gameplay_db, self.modding_log]


def discover() -> GamePaths:
    """Find the game install, the game's user folder (Cache + Logs) and the mod folders."""
    game = _first_existing([os.environ.get("CIVSPEAK_GAME_DIR", "")] + DEFAULT_GAME_DIRS)
    if game is None:
        raise SetupError(
            "Could not find the Civ VI install. Set CIVSPEAK_GAME_DIR to the folder that "
            f"contains Base and DLC (usually .../steamapps/common/{GAME_FOLDER})."
        )

    user = _first_existing(
        [os.environ.get("CIVSPEAK_USER_DIR", "")] + _windows_user_globs(f"AppData/Local/Firaxis Games/{GAME_FOLDER}")
    )
    if user is None:
        raise SetupError(
            "Could not find the game's Cache and Logs folder. Set CIVSPEAK_USER_DIR to "
            f"%LOCALAPPDATA%\\Firaxis Games\\{GAME_FOLDER} (as a /mnt/c/... path)."
        )

    roots = [game / "DLC"]
    workshop = game.parent.parent / "workshop" / "content" / STEAM_APP_ID
    if workshop.is_dir():
        roots.append(workshop)
    user_mods = os.environ.get("CIVSPEAK_MODS_DIR")
    if user_mods:
        roots.append(Path(user_mods))
    else:
        for docs in ("OneDrive/Documents", "Documents"):
            for d in _windows_user_globs(f"{docs}/My Games/{GAME_FOLDER}/Mods"):
                roots.append(Path(d))
    return GamePaths(game_dir=game, user_dir=user, mod_roots=roots)
