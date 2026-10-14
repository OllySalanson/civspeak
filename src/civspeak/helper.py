"""Install the Windows helper (hold ` in Civ VI to ask) from WSL: `civspeak helper install|uninstall|status`.

The helper itself is winhelper.py, run on Windows by Windows' own Python. Installing copies it to
%LOCALAPPDATA%\\Civspeak with a config.json saying how to start this civspeak in WSL, then has the
helper add itself to the programs Windows starts at login (the per-user Run key) and start now.
Only fixed paths are checked; the disk is never searched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path

from .paths import SetupError

FOLDER = "Civspeak"
BRAVE = [
    "/mnt/c/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe",
    "/mnt/c/Program Files (x86)/BraveSoftware/Brave-Browser/Application/brave.exe",
]


def _windows(*cmd: str, timeout: float = 60) -> subprocess.CompletedProcess:
    # Run from a Windows folder, so cmd.exe does not complain about starting in a WSL path.
    try:
        return subprocess.run(list(cmd), cwd="/mnt/c", capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise SetupError(f"Could not run {cmd[0]} on Windows: {e}") from e


def _to_wsl(path: str) -> Path:
    return Path(subprocess.run(["wslpath", "-u", path], capture_output=True, text=True, check=True).stdout.strip())


def _to_windows(path: Path) -> str:
    return subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, check=True).stdout.strip()


def _check_wsl() -> str:
    distro = os.environ.get("WSL_DISTRO_NAME", "")
    if not distro or not os.path.isdir("/mnt/c/Windows"):
        raise SetupError("The Windows helper is installed from WSL on the Windows PC you play on.")
    return distro


def helper_folder() -> Path:
    appdata = _windows("cmd.exe", "/d", "/c", "echo %LOCALAPPDATA%").stdout.strip()
    if not appdata or "%" in appdata:
        raise SetupError("Could not find your Windows AppData folder.")
    return _to_wsl(appdata) / FOLDER


def windows_python() -> str:
    """Windows' own Python 3 (the newest the py launcher knows), as a Windows path."""
    for cmd in (["py.exe", "-3"], ["python.exe"]):
        try:
            found = _windows(*cmd, "-c", "import sys; print(sys.version_info >= (3, 10), sys.executable)", timeout=30)
        except SetupError:
            continue
        ok, _, exe = found.stdout.strip().partition(" ")
        if found.returncode == 0 and ok == "True":
            return exe
    raise SetupError("Python 3.10 or newer for Windows is needed: install it from python.org, then run this again.")


def find_brave(appdata: Path) -> str:
    local = appdata.parent / "BraveSoftware/Brave-Browser/Application/brave.exe"
    for exe in [*BRAVE, str(local)]:
        if os.path.exists(exe):
            return _to_windows(Path(exe))
    return ""


def _helper(python: str, script: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return _windows(python, _to_windows(script), *args, timeout=timeout)


def install(port: int = 8765) -> str:
    distro = _check_wsl()
    python = windows_python()
    folder = helper_folder()
    script = folder / "civspeak_helper.pyw"
    if script.exists():
        _helper(python, script, "stop")  # an older copy may be running: replace it
    folder.mkdir(parents=True, exist_ok=True)
    script.write_bytes(resources.files("civspeak").joinpath("winhelper.py").read_bytes())
    brave = find_brave(folder.parent)
    config = {"distro": distro, "python": sys.executable, "port": port, "brave": brave}
    (folder / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    done = _helper(python, script, "install")
    if done.returncode != 0:
        raise SetupError(f"The helper could not be installed: {done.stderr.strip() or done.stdout.strip()}")
    lines = [
        f"Installed the Civspeak helper in {_to_windows(folder)}.",
        "It is starting now (the page opens on your second screen in a few seconds) and will start every time you log in.",
    ]
    if not brave:
        lines.append("Brave was not found, so the page opens in your default browser instead.")
    return "\n".join(lines)


def uninstall() -> str:
    _check_wsl()
    folder = helper_folder()
    script = folder / "civspeak_helper.pyw"
    if not script.exists():
        return "The Civspeak helper is not installed."
    _helper(windows_python(), script, "uninstall")
    for _ in range(20):  # Brave may take a moment to let go of its profile folder
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return "Removed the Civspeak helper: it is turned off and no longer starts when you log in."
        time.sleep(0.5)
    return f"The helper is turned off and no longer starts at login, but {_to_windows(folder)} could not be deleted."


def status() -> str:
    _check_wsl()
    folder = helper_folder()
    script = folder / "civspeak_helper.pyw"
    if not script.exists():
        return "The Civspeak helper is not installed (civspeak helper install)."
    found = _helper(windows_python(), script, "status")
    try:
        state = json.loads(found.stdout)
    except json.JSONDecodeError:
        return f"Could not ask the helper how it is: {found.stderr.strip()}"
    return (
        f"Helper: {'running' if state['running'] else 'not running'}, "
        f"{'starts' if state['at_login'] else 'does not start'} when you log in ({_to_windows(folder)})."
    )
