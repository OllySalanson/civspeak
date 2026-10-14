"""Where things live: the game install, the game's cache and logs, mods, and civspeak's own files.

Civspeak runs either under Windows' own Python (the Windows app) or in WSL looking at the Windows
drive through /mnt/c. Both see the same Windows PC through a WindowsView, which says where Steam,
Epic, the local app data folder and the Documents folder are and how to turn a Windows path from a
Steam or Epic file into one this process can open.

Everything is found without searching the disk: each location is a fixed, well-known path, a
registry value or a path listed in Steam's or Epic's own files (optionally overridden by an
environment variable), checked with a single shallow glob at most.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from . import system

log = logging.getLogger(__name__)

GAME_FOLDER = "Sid Meier's Civilization VI"
STEAM_APP_ID = "289070"
APP_FOLDER = "Civspeak"
# Civ VII's folders start with the same words, so only these two shapes are Civ VI's.
FIRAXIS_FOLDERS = (GAME_FOLDER, f"{GAME_FOLDER} (*)")
_EPIC_NAME = re.compile(r"civilization vi\b", re.IGNORECASE)


class SetupError(RuntimeError):
    """Something civspeak needs on this PC is missing; the message says what to do."""


def app_dir() -> Path | None:
    """%LOCALAPPDATA%\\Civspeak when running natively on Windows (the Windows app's own folder)."""
    if not system.IS_WINDOWS:
        return None
    base = system.env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    return base / APP_FOLDER


def data_dir() -> Path:
    """The index, spend ledger and timings: %LOCALAPPDATA%\\Civspeak on Windows, ~/.local/share/civspeak elsewhere."""
    if os.environ.get("CIVSPEAK_DATA_DIR"):
        return Path(os.environ["CIVSPEAK_DATA_DIR"])
    app = app_dir()
    if app is not None:
        return app
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "civspeak"


def config_dir() -> Path:
    if os.environ.get("CIVSPEAK_CONFIG_DIR"):
        return Path(os.environ["CIVSPEAK_CONFIG_DIR"])
    app = app_dir()
    if app is not None:
        return app
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "civspeak"


def key_file() -> Path:
    return config_dir() / "anthropic-key"


def spend_file() -> Path:
    return data_dir() / "spend.jsonl"


def latency_file() -> Path:
    return data_dir() / "latency.jsonl"


def cpu_threads() -> int:
    """CPU threads George's voice and the speech recognizer may each use (CIVSPEAK_THREADS, 1-8, default 4).

    Lower it if the game stutters while Civspeak answers; answers then take a little longer.
    """
    try:
        return max(1, min(8, int(os.environ.get("CIVSPEAK_THREADS") or 4)))
    except ValueError:
        return 4


def idle_unload_seconds() -> float:
    """Unload the voice and speech models after this long unused (CIVSPEAK_IDLE_MINUTES, default 20; 0 = never)."""
    try:
        return max(0.0, float(os.environ.get("CIVSPEAK_IDLE_MINUTES") or 20)) * 60
    except ValueError:
        return 20 * 60


def kokoro_files() -> tuple[Path, Path]:
    """The Kokoro model and voice pack: in the app's models folder on Windows, shared with the
    pixel-explainer narrator elsewhere."""
    if os.environ.get("CIVSPEAK_KOKORO_DIR"):
        base = Path(os.environ["CIVSPEAK_KOKORO_DIR"])
    elif app_dir() is not None:
        base = data_dir() / "models"
    else:
        base = Path(os.path.expanduser("~/.local/share/pixel-explainer"))
    return base / "kokoro-v1.0.onnx", base / "voices-v1.0.bin"


def whisper_model() -> str:
    """A faster-whisper model: a folder holding it (in the app's models folder on Windows) or a model
    name read from the Hugging Face cache. Never downloaded at run time."""
    if os.environ.get("CIVSPEAK_WHISPER_MODEL"):
        return os.environ["CIVSPEAK_WHISPER_MODEL"]
    if app_dir() is not None:
        return str(data_dir() / "models" / "whisper-base.en")
    return "base.en"


def sqlite_uri(path: Path, mode: str = "ro") -> str:
    """A SQLite URI that opens `path` read-only, for any path on Windows (drive or UNC) or Linux."""
    from urllib.parse import quote

    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p  # C:/x -> /C:/x, as SQLite expects
    return f"file://{quote(p, safe='/:')}?mode={mode}"


# -- the Windows PC ----------------------------------------------------------------------------


@dataclass
class WindowsView:
    """The Windows PC as this process sees it."""

    to_local: Callable[[str], Path | None]  # a Windows path from a Steam or Epic file -> a path we can open
    steam_dirs: list[Path] = field(default_factory=list)  # where Steam itself is installed, most likely first
    epic_manifests: Path | None = None
    local_appdata: list[Path] = field(default_factory=list)
    documents: list[Path] = field(default_factory=list)

    @classmethod
    def current(cls) -> WindowsView:
        return cls.native() if system.IS_WINDOWS else cls.wsl()

    @classmethod
    def native(cls) -> WindowsView:
        """Windows' own Python: the registry, the environment and the known folders say where things are."""
        steam = [
            system.registry_value("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
            system.registry_value("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            system.registry_value("HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
        for var in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(var):
                steam.append(os.path.join(os.environ[var], "Steam"))
        program_data = system.env_path("ProgramData") or Path("C:/ProgramData")
        local = system.env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        documents = system.known_folder(system.FOLDERID_DOCUMENTS) or Path.home() / "Documents"
        return cls(
            to_local=_native_path,
            # resolve(): Steam writes its path in lower case; this gives the folder's real name.
            steam_dirs=_unique(Path(s).resolve() if os.path.isdir(s) else Path(s) for s in steam if s),
            epic_manifests=program_data / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests",
            local_appdata=[local],
            documents=[documents],
        )

    @classmethod
    def wsl(cls, root: str = "/mnt") -> WindowsView:
        """WSL: the Windows drives are under /mnt. The registry is out of reach, so Steam is looked for
        in its usual folders, and the user folders with one shallow glob over C:\\Users."""
        c = f"{root}/c"
        users = sorted(glob.glob(f"{c}/Users/*/AppData/Local"))
        documents = []
        for docs in ("OneDrive/Documents", "Documents"):
            documents += sorted(glob.glob(f"{c}/Users/*/{docs}"))
        return cls(
            to_local=lambda p: _wsl_path(p, root),
            steam_dirs=[Path(f"{c}/Program Files (x86)/Steam"), Path(f"{c}/Program Files/Steam")],
            epic_manifests=Path(f"{c}/ProgramData/Epic/EpicGamesLauncher/Data/Manifests"),
            local_appdata=[Path(u) for u in users],
            documents=[Path(d) for d in documents],
        )


def _native_path(path: str) -> Path | None:
    return Path(path) if path else None


def _wsl_path(path: str, root: str = "/mnt") -> Path | None:
    """C:\\Games\\Steam -> /mnt/c/Games/Steam; None for network paths WSL cannot reach this way."""
    win = PureWindowsPath(path.replace("/", "\\"))
    if len(win.drive) != 2 or win.drive[1] != ":":
        return None
    return Path(root, win.drive[0].lower(), *win.parts[1:])


def _unique(paths) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        key = os.path.normcase(os.path.normpath(str(p)))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _shallow(base: Path, *patterns: str) -> list[Path]:
    """The folders matching each pattern directly inside base (one glob, never recursive)."""
    out = []
    for pattern in patterns:
        out += [Path(p) for p in sorted(glob.glob(os.path.join(glob.escape(str(base)), pattern))) if os.path.isdir(p)]
    return out


# -- Steam ---------------------------------------------------------------------------------------

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|\s+')


def parse_vdf(text: str) -> dict:
    """Steam's KeyValues text format (libraryfolders.vdf, appmanifest_*.acf) as nested dicts."""
    root: dict = {}
    stack = [root]
    key = None
    for m in _VDF_TOKEN.finditer(text):
        quoted, brace = m.group(1), m.group(2)
        if brace == "{":
            child: dict = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif quoted is not None:
            value = re.sub(r"\\(.)", lambda e: {"n": "\n", "t": "\t"}.get(e.group(1), e.group(1)), quoted)
            if key is None:
                key = value
            else:
                stack[-1][key] = value
                key = None
    return root


def _read_vdf(path: Path) -> dict:
    try:
        return parse_vdf(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def steam_libraries(steam_dir: Path, view: WindowsView) -> list[Path]:
    """Every Steam library (on any drive) listed in Steam's libraryfolders.vdf, Steam's own folder first."""
    libraries = [steam_dir]
    folders = _read_vdf(steam_dir / "steamapps" / "libraryfolders.vdf")
    folders = folders.get("libraryfolders") or folders.get("LibraryFolders") or {}
    for entry in folders.values():
        # Current Steam: {"path": "D:\\SteamLibrary", "apps": {...}}; older Steam: "1" "D:\\SteamLibrary".
        where = entry.get("path") if isinstance(entry, dict) else entry if isinstance(entry, str) else None
        if where and ("\\" in where or "/" in where):
            local = view.to_local(where)
            if local is not None:
                libraries.append(local)
    return _unique(libraries)


def steam_game_dirs(view: WindowsView) -> list[Path]:
    """Civ VI folders in the Steam libraries that have it installed (its appmanifest says where)."""
    found = []
    for steam in view.steam_dirs:
        for library in steam_libraries(steam, view):
            apps = library / "steamapps"
            manifest = apps / f"appmanifest_{STEAM_APP_ID}.acf"
            if manifest.is_file():
                folder = _read_vdf(manifest).get("AppState", {}).get("installdir") or GAME_FOLDER
                found.append(apps / "common" / folder)
            elif (apps / "common" / GAME_FOLDER).is_dir():
                found.append(apps / "common" / GAME_FOLDER)
    return _unique(p for p in found if p.is_dir())


# -- Epic Games ----------------------------------------------------------------------------------


def epic_game_dirs(view: WindowsView) -> list[Path]:
    """Civ VI folders installed by the Epic Games launcher, from the launcher's *.item manifests."""
    if view.epic_manifests is None or not view.epic_manifests.is_dir():
        return []
    found = []
    for item in sorted(view.epic_manifests.glob("*.item")):
        try:
            manifest = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        location = str(manifest.get("InstallLocation") or "")
        names = [str(manifest.get("DisplayName") or ""), PureWindowsPath(location).name]
        if not location or not any(_EPIC_NAME.search(n.replace("\u2019", "'")) for n in names):
            continue
        local = view.to_local(location)
        if local is not None and (local / "Base").is_dir():
            found.append(local)
    return _unique(found)


# -- the game's own user folders -----------------------------------------------------------------


def firaxis_user_dirs(view: WindowsView) -> list[Path]:
    """The game's Cache and Logs folders (Steam's, and Epic's if it has its own), the one with the
    newest Modding.log first: that is the game the player loaded last."""
    found = []
    for local in view.local_appdata:
        found += _shallow(local / "Firaxis Games", *FIRAXIS_FOLDERS)
    return sorted(_unique(found), key=_modding_log_time, reverse=True)


def _modding_log_time(user_dir: Path) -> float:
    try:
        return (user_dir / "Logs" / "Modding.log").stat().st_mtime
    except OSError:
        return 0.0


def local_mod_dirs(view: WindowsView) -> list[Path]:
    """Mods installed by hand: Documents\\My Games\\<game>\\Mods (Epic's folder is "<game> (Epic)")."""
    found = []
    for docs in view.documents:
        found += [p / "Mods" for p in _shallow(docs / "My Games", *FIRAXIS_FOLDERS) if (p / "Mods").is_dir()]
    return _unique(found)


def read_display_language(user_dir: Path) -> str:
    """The language the game shows its text in (AppOptions.txt's DisplayLanguage, e.g. en_US), or ""."""
    try:
        text = (user_dir / "AppOptions.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        parts = line.split(";", 1)[0].split()
        if len(parts) == 2 and parts[0] == "DisplayLanguage":
            return parts[1]
    return ""


# -- putting it together -------------------------------------------------------------------------


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

    @property
    def display_language(self) -> str:
        """The game's text language (for example de_DE). Civspeak builds the English text either way."""
        return read_display_language(self.user_dir)

    def sources(self) -> list[Path]:
        """Files whose change means the index is out of date."""
        return [self.gameplay_db, self.modding_log]


def _env_dir(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value and os.path.isdir(value) else None


def discover(view: WindowsView | None = None) -> GamePaths:
    """Find the game install, the game's user folder (Cache + Logs) and the mod folders."""
    view = view or WindowsView.current()

    user = _env_dir("CIVSPEAK_USER_DIR")
    if user is None:
        users = firaxis_user_dirs(view)
        if not users:
            looked = ", ".join(str(p / "Firaxis Games" / GAME_FOLDER) for p in view.local_appdata) or "%LOCALAPPDATA%"
            raise SetupError(
                f"Could not find the game's Cache and Logs folder (looked in {looked}). Start Civ VI once, "
                "or set CIVSPEAK_USER_DIR to the folder that holds Cache and Logs."
            )
        user = users[0]

    game = _env_dir("CIVSPEAK_GAME_DIR")
    if game is None:
        steam, epic = steam_game_dirs(view), epic_game_dirs(view)
        # The Epic build keeps its own "(Epic)" user folder: when that is the newest, so is the Epic install.
        installs = epic + steam if user.name.endswith("(Epic)") else steam + epic
        if not installs:
            looked = ", ".join(str(p) for p in view.steam_dirs) or "no Steam folder"
            raise SetupError(
                f"Could not find the Civ VI install (looked in the Steam libraries of {looked}, and in Epic Games). "
                f"Set CIVSPEAK_GAME_DIR to the folder that contains Base and DLC (usually .../steamapps/common/{GAME_FOLDER})."
            )
        game = installs[0]

    roots = [game / "DLC"]
    workshop = game.parent.parent / "workshop" / "content" / STEAM_APP_ID
    if workshop.is_dir():
        roots.append(workshop)
    user_mods = os.environ.get("CIVSPEAK_MODS_DIR")
    roots += [Path(user_mods)] if user_mods else local_mod_dirs(view)
    return GamePaths(game_dir=game, user_dir=user, mod_roots=roots)
