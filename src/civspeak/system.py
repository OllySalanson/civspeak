"""What differs between Windows' own Python (the Windows app) and Linux or WSL: process priority,
how to start a child civspeak process, and the folders and registry values Windows knows.

Standard library only. ctypes and winreg are imported inside the Windows-only functions, so this
module imports on Linux too and the tests can replace those calls with fakes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000
# FOLDERID_Documents: the Documents folder wherever the player (or OneDrive) moved it.
FOLDERID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"


def lower_priority() -> None:
    """Run this process below everything else, so the game stays smooth: below normal priority on
    Windows, nice 10 elsewhere. Child processes inherit it."""
    if IS_WINDOWS:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
        return
    try:
        os.nice(10)
    except OSError:
        pass


def civspeak_command(command: str) -> list[str]:
    """The command line that runs `civspeak <command>` in a new process. In the packaged Windows app
    sys.executable is Civspeak.exe, which has no -m, so it is run as `Civspeak.exe --<command>`."""
    if getattr(sys, "frozen", False):
        return [sys.executable, f"--{command}"]
    return [sys.executable, "-m", "civspeak", command]


def background_process_options() -> dict:
    """subprocess options for a helper process that must never get in the game's way: on Windows it
    starts below normal priority and without a console window flashing up."""
    if IS_WINDOWS:
        return {"creationflags": BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW}
    return {}


def registry_value(hive: str, key: str, name: str) -> str | None:
    """A string value from the Windows registry (hive such as "HKEY_CURRENT_USER"), or None."""
    if not IS_WINDOWS:
        return None
    import winreg

    try:
        with winreg.OpenKey(getattr(winreg, hive), key) as k:
            value, kind = winreg.QueryValueEx(k, name)
    except OSError:
        return None
    return value if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and value else None


def known_folder(folder_id: str) -> Path | None:
    """A Windows known folder (such as FOLDERID_DOCUMENTS) where it really is, or None."""
    if not IS_WINDOWS:
        return None
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    raw = uuid.UUID(folder_id).bytes_le
    guid = GUID.from_buffer_copy(raw)
    found = ctypes.c_wchar_p()
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    try:
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(found)) != 0:
            return None
        return Path(found.value) if found.value else None
    finally:
        ole32.CoTaskMemFree(found)


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None
