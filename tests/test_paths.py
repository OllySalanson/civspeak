"""Finding Civ VI on any Windows PC: Steam on any drive, Epic, a moved Documents folder, the newest
game folder, the game's language. Every folder, registry value and file here is invented."""

import json
import os
import sqlite3
import sys
from pathlib import Path, PureWindowsPath

import pytest

from civspeak import paths, system
from civspeak.paths import WindowsView

GAME = paths.GAME_FOLDER
EPIC_CIV = r"F:\Epic Games\SidMeiersCivilizationVI"

LIBRARY_FOLDERS = r""""libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"label"		""
		"apps"
		{
			"228980"		"285020366"
		}
	}
	// a second library on another drive, where the game is
	"1"
	{
		"path"		"D:\\Games\\SteamLibrary"
		"apps"
		{
			"289070"		"28049031644"
		}
	}
	"2"
	{
		"path"		"\\\\nas\\steam"
	}
}
"""

APP_MANIFEST = r""""AppState"
{
	"appid"		"289070"
	"name"		"Sid Meier's Civilization VI"
	"installdir"		"Civ Six Folder"
}
"""

APP_OPTIONS = """;Altering this number will cause the app to overwrite this file with defaults\r
Version 15\r
\r
[Language]\r
;The language to use for all text in-game.\r
;DisplayLanguage xx_XX\r
DisplayLanguage de_DE\r
AudioLanguage German\r
"""


class FakePC:
    """An invented Windows PC under tmp_path: C: and D: drives as folders."""

    def __init__(self, root: Path):
        self.root = root

    def drive(self, letter: str) -> Path:
        return self.root / letter.lower()

    def local(self, win_path: str) -> Path | None:
        win = PureWindowsPath(win_path)
        if len(win.drive) != 2:
            return None
        return self.drive(win.drive[0]).joinpath(*win.parts[1:])

    def mkdir(self, win_path: str) -> Path:
        p = self.local(win_path)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write(self, win_path: str, text: str) -> Path:
        p = self.local(win_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def game(self, win_path: str) -> Path:
        game = self.mkdir(win_path)
        (game / "Base").mkdir()
        (game / "DLC").mkdir()
        return game

    def firaxis(self, folder: str, log_age: float | None) -> Path:
        user = self.mkdir(rf"C:\Users\Player\AppData\Local\Firaxis Games\{folder}")
        (user / "Cache").mkdir()
        if log_age is not None:
            log = user / "Logs" / "Modding.log"
            log.parent.mkdir()
            log.write_text("invented log\n")
            os.utime(log, (1_700_000_000 - log_age, 1_700_000_000 - log_age))
        return user

    def view(self, steam=(r"C:\Program Files (x86)\Steam",), epic=True) -> WindowsView:
        return WindowsView(
            to_local=self.local,
            steam_dirs=[self.local(s) for s in steam],
            epic_manifests=self.local(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests") if epic else None,
            local_appdata=[self.local(r"C:\Users\Player\AppData\Local")],
            documents=[self.local(r"C:\Users\Player\OneDrive\Documents")],
        )


@pytest.fixture
def pc(tmp_path, monkeypatch):
    for name in ("CIVSPEAK_GAME_DIR", "CIVSPEAK_USER_DIR", "CIVSPEAK_MODS_DIR"):
        monkeypatch.delenv(name, raising=False)
    return FakePC(tmp_path / "pc")


def steam_on_d(pc: FakePC) -> Path:
    pc.write(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf", LIBRARY_FOLDERS)
    pc.write(r"D:\Games\SteamLibrary\steamapps\appmanifest_289070.acf", APP_MANIFEST)
    game = pc.game(r"D:\Games\SteamLibrary\steamapps\common\Civ Six Folder")
    pc.mkdir(r"D:\Games\SteamLibrary\steamapps\workshop\content\289070\111")
    return game


def test_vdf_reads_nested_blocks_escapes_and_old_style_entries():
    data = paths.parse_vdf(LIBRARY_FOLDERS)
    assert data["libraryfolders"]["1"]["path"] == r"D:\Games\SteamLibrary"
    assert data["libraryfolders"]["1"]["apps"] == {"289070": "28049031644"}
    assert data["libraryfolders"]["2"]["path"] == r"\\nas\steam"
    old = paths.parse_vdf('"LibraryFolders"\n{\n\t"TimeNextStatsReport"\t"1"\n\t"1"\t"E:\\\\Steam"\n}\n')
    assert old["LibraryFolders"]["1"] == r"E:\Steam"


def test_steam_library_on_another_drive_is_found_from_libraryfolders(pc):
    game = steam_on_d(pc)
    pc.firaxis(GAME, log_age=0)
    found = paths.discover(pc.view())
    assert found.game_dir == game
    # Workshop mods come from the library the game is in.
    assert pc.local(r"D:\Games\SteamLibrary\steamapps\workshop\content\289070") in found.mod_roots
    assert found.mod_roots[0] == game / "DLC"


def test_steam_folder_from_the_registry(pc, monkeypatch):
    # Steam installed somewhere unusual: only its registry value says where.
    pc.write(r"E:\Odd\Steam\steamapps\appmanifest_289070.acf", APP_MANIFEST.replace("Civ Six Folder", GAME))
    game = pc.game(rf"E:\Odd\Steam\steamapps\common\{GAME}")
    values = {("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"): str(pc.local(r"E:\Odd\Steam"))}
    monkeypatch.setattr(system, "registry_value", lambda hive, key, name: values.get((hive, key, name)))
    monkeypatch.setattr(system, "known_folder", lambda folder_id: Path("/nowhere/Documents"))
    monkeypatch.setenv("LOCALAPPDATA", str(pc.local(r"C:\Users\Player\AppData\Local")))
    monkeypatch.setenv("ProgramFiles(x86)", str(pc.local(r"C:\Program Files (x86)")))
    monkeypatch.delenv("ProgramFiles", raising=False)
    view = WindowsView.native()
    assert view.steam_dirs[0] == pc.local(r"E:\Odd\Steam")
    assert paths.steam_game_dirs(view) == [game]
    assert view.local_appdata == [pc.local(r"C:\Users\Player\AppData\Local")]
    assert view.documents == [Path("/nowhere/Documents")]


def test_a_game_folder_without_a_manifest_still_counts_and_missing_ones_do_not(pc):
    game = pc.game(rf"C:\Program Files (x86)\Steam\steamapps\common\{GAME}")
    pc.write(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf", LIBRARY_FOLDERS)
    # D: lists the game but it was deleted by hand: its manifest is there, the folder is not.
    pc.write(r"D:\Games\SteamLibrary\steamapps\appmanifest_289070.acf", APP_MANIFEST)
    assert paths.steam_game_dirs(pc.view()) == [game]


def epic_item(pc: FakePC, name: str, manifest: dict) -> None:
    pc.write(rf"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests\{name}.item", json.dumps(manifest))


def test_epic_install_is_found_from_the_launcher_manifests(pc):
    game = pc.game(EPIC_CIV)
    epic_item(pc, "A1", {"DisplayName": "Sid Meier\u2019s Civilization VI", "InstallLocation": EPIC_CIV})
    pc.game(r"F:\Epic Games\CivilizationVII")
    epic_item(pc, "B2", {"DisplayName": "Sid Meier's Civilization VII", "InstallLocation": r"F:\Epic Games\CivilizationVII"})
    epic_item(pc, "C3", {"DisplayName": "Some Other Game", "InstallLocation": r"F:\Epic Games\Other"})
    pc.write(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests\broken.item", "{not json")
    assert paths.epic_game_dirs(pc.view()) == [game]

    epic_user = pc.firaxis(f"{GAME} (Epic)", log_age=0)
    epic_mods = pc.mkdir(rf"C:\Users\Player\OneDrive\Documents\My Games\{GAME} (Epic)\Mods")
    found = paths.discover(pc.view())
    assert (found.game_dir, found.user_dir) == (game, epic_user)
    assert found.mod_roots == [game / "DLC", epic_mods]


def test_the_newest_modding_log_picks_the_user_folder_and_civ_vii_is_ignored(pc):
    steam_on_d(pc)
    pc.firaxis(GAME, log_age=3600)
    epic = pc.firaxis(f"{GAME} (Epic)", log_age=60)
    pc.firaxis(f"{GAME}I", log_age=0)  # Civilization VII: newest of all, but another game
    pc.firaxis(f"{GAME} Unrelated", log_age=0)
    assert paths.firaxis_user_dirs(pc.view())[0] == epic
    assert [p.name for p in paths.firaxis_user_dirs(pc.view())] == [f"{GAME} (Epic)", GAME]


def test_steam_wins_when_the_steam_game_was_played_last(pc):
    steam_game = steam_on_d(pc)
    pc.game(EPIC_CIV)
    epic_item(pc, "A1", {"DisplayName": "Sid Meier's Civilization VI", "InstallLocation": EPIC_CIV})
    user = pc.firaxis(GAME, log_age=0)
    pc.firaxis(f"{GAME} (Epic)", log_age=3600)
    found = paths.discover(pc.view())
    assert (found.game_dir, found.user_dir) == (steam_game, user)


def test_documents_mods_follow_the_known_folder(pc):
    steam_on_d(pc)
    pc.firaxis(GAME, log_age=0)
    mods = pc.mkdir(rf"C:\Users\Player\OneDrive\Documents\My Games\{GAME}\Mods")
    pc.mkdir(rf"C:\Users\Player\OneDrive\Documents\My Games\{GAME}I\Mods")  # Civ VII's
    assert paths.discover(pc.view()).mod_roots[-1:] == [mods]


def test_environment_overrides_win(pc, monkeypatch, tmp_path):
    steam_on_d(pc)
    pc.firaxis(GAME, log_age=0)
    mine = tmp_path / "my-game"
    (mine / "DLC").mkdir(parents=True)
    user = tmp_path / "my-user"
    user.mkdir()
    monkeypatch.setenv("CIVSPEAK_GAME_DIR", str(mine))
    monkeypatch.setenv("CIVSPEAK_USER_DIR", str(user))
    monkeypatch.setenv("CIVSPEAK_MODS_DIR", str(tmp_path / "my-mods"))
    found = paths.discover(pc.view())
    assert (found.game_dir, found.user_dir) == (mine, user)
    assert found.mod_roots == [mine / "DLC", tmp_path / "my-mods"]


def test_nothing_found_says_where_it_looked(pc):
    with pytest.raises(paths.SetupError, match="Cache and Logs"):
        paths.discover(pc.view())
    pc.firaxis(GAME, log_age=0)
    with pytest.raises(paths.SetupError, match=r"Steam libraries of .*Steam.*CIVSPEAK_GAME_DIR"):
        paths.discover(pc.view(epic=False))


def test_display_language_from_app_options(pc):
    user = pc.firaxis(GAME, log_age=0)
    game = paths.GamePaths(game_dir=pc.root, user_dir=user)
    assert game.display_language == ""
    (user / "AppOptions.txt").write_bytes(APP_OPTIONS.encode())
    assert game.display_language == "de_DE"


def test_wsl_turns_windows_paths_into_mnt_paths(tmp_path):
    assert paths._wsl_path(r"D:\Games\SteamLibrary") == Path("/mnt/d/Games/SteamLibrary")
    assert paths._wsl_path("c:/program files (x86)/steam") == Path("/mnt/c/program files (x86)/steam")
    assert paths._wsl_path(r"\\nas\steam") is None
    assert paths._wsl_path("relative") is None
    # The WSL view globs the Windows users once, shallowly.
    (tmp_path / "c" / "Users" / "Player" / "AppData" / "Local").mkdir(parents=True)
    (tmp_path / "c" / "Users" / "Player" / "Documents").mkdir(parents=True)
    view = WindowsView.wsl(root=str(tmp_path))
    assert view.local_appdata == [tmp_path / "c" / "Users" / "Player" / "AppData" / "Local"]
    assert view.documents == [tmp_path / "c" / "Users" / "Player" / "Documents"]
    assert view.steam_dirs[0] == tmp_path / "c" / "Program Files (x86)" / "Steam"
    assert view.to_local(r"E:\x") == tmp_path / "e" / "x"


def test_windows_app_folders(monkeypatch, tmp_path):
    monkeypatch.delenv("CIVSPEAK_DATA_DIR")
    monkeypatch.delenv("CIVSPEAK_CONFIG_DIR")
    for name in ("CIVSPEAK_KOKORO_DIR", "CIVSPEAK_WHISPER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    app = tmp_path / "Local" / "Civspeak"
    assert paths.data_dir() == paths.config_dir() == app
    assert paths.key_file() == app / "anthropic-key"
    assert paths.kokoro_files() == (app / "models" / "kokoro-v1.0.onnx", app / "models" / "voices-v1.0.bin")
    assert paths.whisper_model() == str(app / "models" / "whisper-base.en")
    monkeypatch.setattr(system, "IS_WINDOWS", False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert paths.data_dir() == tmp_path / "share" / "civspeak"
    assert paths.whisper_model() == "base.en"


def test_sqlite_uri_opens_awkward_paths_read_only(tmp_path):
    folder = tmp_path / "Sid Meier's #1 100% folder"
    folder.mkdir()
    db_path = folder / "x.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE t(v)")
    db.commit()
    db.close()
    ro = sqlite3.connect(paths.sqlite_uri(db_path), uri=True)
    assert ro.execute("SELECT count(*) FROM t").fetchone() == (0,)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")
    ro.close()


def test_child_civspeak_commands(monkeypatch):
    assert system.civspeak_command("build") == [sys.executable, "-m", "civspeak", "build"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert system.civspeak_command("build") == [sys.executable, "--build"]
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    flags = system.background_process_options()["creationflags"]
    assert flags & system.BELOW_NORMAL_PRIORITY_CLASS and flags & system.CREATE_NO_WINDOW
