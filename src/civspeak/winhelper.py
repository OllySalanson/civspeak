"""Civspeak's Windows helper: hold ` while Civ VI has the focus to ask, without the game seeing the key.

This file runs on Windows under pythonw (no console window, standard library only). In WSL,
`civspeak helper install` copies it to %LOCALAPPDATA%\\Civspeak\\civspeak_helper.pyw with a config.json
beside it, and has Windows start it when you log in. It:

- starts the Civspeak server in WSL (through wsl.exe) and opens the answer page as its own Brave
  window filling the second screen;
- while Civ VI has the focus, takes the ` key (the key under Esc, on any keyboard layout) and tells
  the page to listen until the key is let go;
- shows a star icon by the clock with a small menu: open the page, start when I log in, turn off;
- installs Civspeak shortcuts in the Start menu and on the desktop, which start it (or, when it is
  already running, bring its page to the front), so no terminal is ever needed.

How the key is taken: RegisterHotKey. Windows hands that one key press to this program instead of
the game, and nothing else: unlike a keyboard hook, no other key you type passes through here, so
the game's input is never slowed. RegisterHotKey reports the press but not the release, so while the
key is held a 20 ms timer checks it with GetAsyncKeyState. The rest of the time the helper sleeps in
GetMessage and uses no CPU. The key is only taken while the game has the focus (the helper is told
when the focused window changes), so ` types normally everywhere else.
"""

from __future__ import annotations

import json
import ntpath
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

NAME = "Civspeak"
SCRIPT_NAME = "civspeak_helper.pyw"
WINDOW_CLASS = "CivspeakHelper"
MUTEX_NAME = "Local\\CivspeakHelper"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
GAMES = ("CivilizationVI.exe", "CivilizationVI_DX12.exe")
TALK_SCAN_CODE = 0x29  # the key under Esc: ` on US and UK keyboards (its key code differs between them)
POLL_MS = 20  # how often the key is checked while it is held
SERVER_START_SECONDS = 180  # at login WSL itself may still be starting
PAGE_TITLE = "Civspeak"  # the page's <title>, which is also its app window's title
STATUS_STARTING = "Civspeak is starting..."
STATUS_READY = "Civspeak: hold ` in Civ to ask"
STATUS_STOPPED = "Civspeak stopped (see server.log)"

BRAVE_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    # Holding ` in the game starts the microphone and George's voice without a click on the page first.
    "--autoplay-policy=no-user-gesture-required",
    # Keep the page fully awake while the game has the focus, even if the game covers it.
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
)


@dataclass
class Config:
    distro: str  # the WSL distribution civspeak is installed in
    python: str  # civspeak's Python in WSL (a Linux path)
    port: int = 8765
    brave: str = ""  # brave.exe; the default browser is used when it is empty
    games: list[str] = field(default_factory=lambda: list(GAMES))
    folder: Path = Path(".")

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def profile(self) -> Path:
        # Its own Brave profile: its own process (so the flags above apply) and its own microphone permission.
        return self.folder / "brave-profile"


def load_config(folder: Path) -> Config:
    data = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    return Config(
        distro=data["distro"],
        python=data["python"],
        port=int(data.get("port", 8765)),
        brave=data.get("brave", ""),
        games=list(data.get("games", GAMES)),
        folder=folder,
    )


def server_command(cfg: Config) -> list[str]:
    """Runs `civspeak serve` in WSL; it stops when this helper closes its standard input."""
    serve = [cfg.python, "-u", "-m", "civspeak", "serve", "--port", str(cfg.port), "--watch-stdin"]
    return ["wsl.exe", "-d", cfg.distro, "--cd", "~", "-e", *serve]


def brave_command(cfg: Config, area: tuple[int, int, int, int]) -> list[str]:
    left, top, right, bottom = area
    return [
        cfg.brave,
        f"--user-data-dir={cfg.profile}",
        f"--app={cfg.url}",
        f"--window-position={left},{top}",
        f"--window-size={right - left},{bottom - top}",
        *BRAVE_FLAGS,
    ]


def pick_screen(screens: list[tuple[tuple[int, int, int, int], bool]]) -> tuple[int, int, int, int]:
    """The work area of the first screen that is not the main one (the main one when there is only one)."""
    others = [area for area, primary in screens if not primary]
    return (others or [area for area, _ in screens] or [(0, 0, 1280, 800)])[0]


def is_game(exe: str, games: list[str]) -> bool:
    return bool(exe) and ntpath.basename(exe).lower() in {g.lower() for g in games}


class Hold:
    """Turns "the key went down" (the hotkey) and "is it still down?" (the timer) into down and up."""

    def __init__(self, send):
        self.send = send
        self.held = False

    def pressed(self) -> bool:
        """The hotkey fired. True when the timer should start watching for the release."""
        if self.held:
            return False
        self.held = True
        self.send("down")
        return True

    def tick(self, still_down: bool) -> bool:
        """A timer tick. True when the timer should stop."""
        if not self.held:
            return True
        if still_down:
            return False
        self.held = False
        self.send("up")
        return True


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post_key(api: str, key: str, timeout: float = 3.0) -> bool:
    """Tell the server the key went down or up. True when an open page took it."""
    req = urllib.request.Request(
        f"{api}/api/hotkey",
        data=json.dumps({"key": key}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _NO_PROXY.open(req, timeout=timeout) as reply:
            return bool(json.load(reply).get("listening"))
    except (OSError, ValueError):
        return False


def server_alive(api: str, timeout: float = 1.0) -> bool:
    try:
        with _NO_PROXY.open(f"{api}/api/status", timeout=timeout) as reply:
            return reply.status == 200
    except (OSError, ValueError):
        return False


class Messenger:
    """Posts key changes to the server in order on its own thread, so the message loop never waits."""

    def __init__(self, api: str, on_unheard):
        self.api = api
        self.on_unheard = on_unheard  # called (on this thread) when nobody was listening for a press
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        threading.Thread(target=self._run, name="talk", daemon=True).start()

    def send(self, key: str) -> None:
        self.queue.put(key)

    def _run(self) -> None:
        while True:
            key = self.queue.get()
            if not post_key(self.api, key) and key == "down":
                self.on_unheard()


# The page's favicon (a gold star on a navy rounded square, drawn on a 32 x 32 grid) for the tray icon.
ICON_NAVY = (0x13, 0x21, 0x3A)
ICON_GOLD = (0xE0, 0xBB, 0x63)
ICON_STAR = [
    (16, 5),
    (19.2, 12.3),
    (27, 12.9),
    (21, 18),
    (22.9, 25.7),
    (16, 21.6),
    (9.1, 25.7),
    (11, 18),
    (5, 12.9),
    (12.8, 12.3),
]


def _in_rounded_square(x: float, y: float, size: float = 32, radius: float = 7) -> bool:
    if not (0 <= x <= size and 0 <= y <= size):
        return False
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def _in_polygon(x: float, y: float, points: list[tuple[float, float]]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def icon_pixels(size: int, samples: int = 4) -> bytes:
    """The icon at size x size pixels, top row first, as BGRA bytes (anti-aliased by supersampling)."""
    out = bytearray()
    scale = 32 / size
    for y in range(size):
        for x in range(size):
            square = star = 0
            for sy in range(samples):
                for sx in range(samples):
                    px = (x + (sx + 0.5) / samples) * scale
                    py = (y + (sy + 0.5) / samples) * scale
                    if _in_rounded_square(px, py):
                        square += 1
                        star += _in_polygon(px, py, ICON_STAR)
            gold = star / square if square else 0
            r, g, b = (round(n + (s - n) * gold) for n, s in zip(ICON_NAVY, ICON_GOLD, strict=True))
            out += bytes((b, g, r, round(255 * square / samples**2)))
    return bytes(out)


def ico_file(sizes: tuple[int, ...] = (16, 24, 32, 48, 256)) -> bytes:
    """The icon as a .ico file, for the Start menu and desktop shortcuts."""
    import struct

    images = []
    for size in sizes:
        pixels = icon_pixels(size, samples=4 if size <= 48 else 2)
        rows = [pixels[y * size * 4 : (y + 1) * size * 4] for y in range(size)]
        mask = bytes((size + 31) // 32 * 4 * size)  # all clear: the alpha channel decides
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        images.append(header + b"".join(reversed(rows)) + mask)  # bitmaps are stored bottom row first
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, image in zip(sizes, images, strict=True):
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    return out + b"".join(images)


# -- Windows ---------------------------------------------------------------------------------
# Everything below runs on Windows only (ctypes.windll); the logic above is tested on Linux.

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_HOTKEY = 0x0312
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_NULL = 0x0000
WM_APP = 0x8000
WM_TRAY = WM_APP + 1  # the tray icon was clicked
WM_OPEN_PAGE = WM_APP + 2  # another copy of the helper was started: show the page
WM_QUIT_HELPER = WM_APP + 3  # `civspeak helper uninstall`: turn off
WM_SERVER = WM_APP + 4  # wParam: one of the SERVER_* below
WM_UNHEARD = WM_APP + 5  # ` was pressed but no page was listening
SERVER_READY, SERVER_FAILED, SERVER_STOPPED = 1, 2, 3

HOTKEY_ID = 1
TIMER_ID = 1
MENU_OPEN, MENU_STARTUP, MENU_OFF = 1, 2, 3


def _win32():
    """ctypes bindings for the few Windows functions the helper uses."""
    import ctypes
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    LRESULT = w.LPARAM
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    WINEVENTPROC = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)
    WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    MONITORENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", w.UINT),
            ("style", w.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", w.HINSTANCE),
            ("hIcon", w.HICON),
            ("hCursor", w.HANDLE),
            ("hbrBackground", w.HBRUSH),
            ("lpszMenuName", w.LPCWSTR),
            ("lpszClassName", w.LPCWSTR),
            ("hIconSm", w.HICON),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", w.DWORD),
            ("hWnd", w.HWND),
            ("uID", w.UINT),
            ("uFlags", w.UINT),
            ("uCallbackMessage", w.UINT),
            ("hIcon", w.HICON),
            ("szTip", w.WCHAR * 128),
            ("dwState", w.DWORD),
            ("dwStateMask", w.DWORD),
            ("szInfo", w.WCHAR * 256),
            ("uVersion", w.UINT),
            ("szInfoTitle", w.WCHAR * 64),
            ("dwInfoFlags", w.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", w.HICON),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", w.DWORD),
            ("biWidth", w.LONG),
            ("biHeight", w.LONG),
            ("biPlanes", w.WORD),
            ("biBitCount", w.WORD),
            ("biCompression", w.DWORD),
            ("biSizeImage", w.DWORD),
            ("biXPelsPerMeter", w.LONG),
            ("biYPelsPerMeter", w.LONG),
            ("biClrUsed", w.DWORD),
            ("biClrImportant", w.DWORD),
        ]

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", w.BOOL),
            ("xHotspot", w.DWORD),
            ("yHotspot", w.DWORD),
            ("hbmMask", w.HBITMAP),
            ("hbmColor", w.HBITMAP),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", w.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", w.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", w.DWORD),
            ("SchedulingClass", w.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def sig(fn, restype, *argtypes):
        fn.restype, fn.argtypes = restype, argtypes
        return fn

    P = ctypes.POINTER
    api = {
        "RegisterClassExW": sig(user32.RegisterClassExW, w.ATOM, P(WNDCLASSEXW)),
        "CreateWindowExW": sig(
            user32.CreateWindowExW,
            w.HWND,
            w.DWORD,
            w.LPCWSTR,
            w.LPCWSTR,
            w.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            w.HWND,
            w.HMENU,
            w.HINSTANCE,
            w.LPVOID,
        ),  # fmt: skip
        "DestroyWindow": sig(user32.DestroyWindow, w.BOOL, w.HWND),
        "DefWindowProcW": sig(user32.DefWindowProcW, LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM),
        "GetMessageW": sig(user32.GetMessageW, w.BOOL, P(w.MSG), w.HWND, w.UINT, w.UINT),
        "TranslateMessage": sig(user32.TranslateMessage, w.BOOL, P(w.MSG)),
        "DispatchMessageW": sig(user32.DispatchMessageW, LRESULT, P(w.MSG)),
        "PostMessageW": sig(user32.PostMessageW, w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM),
        "PostQuitMessage": sig(user32.PostQuitMessage, None, ctypes.c_int),
        "FindWindowW": sig(user32.FindWindowW, w.HWND, w.LPCWSTR, w.LPCWSTR),
        "RegisterWindowMessageW": sig(user32.RegisterWindowMessageW, w.UINT, w.LPCWSTR),
        "RegisterHotKey": sig(user32.RegisterHotKey, w.BOOL, w.HWND, ctypes.c_int, w.UINT, w.UINT),
        "UnregisterHotKey": sig(user32.UnregisterHotKey, w.BOOL, w.HWND, ctypes.c_int),
        "MapVirtualKeyW": sig(user32.MapVirtualKeyW, w.UINT, w.UINT, w.UINT),
        "GetAsyncKeyState": sig(user32.GetAsyncKeyState, w.SHORT, ctypes.c_int),
        "SetTimer": sig(user32.SetTimer, ctypes.c_size_t, w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID),
        "KillTimer": sig(user32.KillTimer, w.BOOL, w.HWND, ctypes.c_size_t),
        "SetWinEventHook": sig(
            user32.SetWinEventHook, w.HANDLE, w.DWORD, w.DWORD, w.HMODULE, WINEVENTPROC, w.DWORD, w.DWORD, w.DWORD
        ),
        "UnhookWinEvent": sig(user32.UnhookWinEvent, w.BOOL, w.HANDLE),
        "GetForegroundWindow": sig(user32.GetForegroundWindow, w.HWND),
        "SetForegroundWindow": sig(user32.SetForegroundWindow, w.BOOL, w.HWND),
        "ShowWindow": sig(user32.ShowWindow, w.BOOL, w.HWND, ctypes.c_int),
        "IsIconic": sig(user32.IsIconic, w.BOOL, w.HWND),
        "IsWindowVisible": sig(user32.IsWindowVisible, w.BOOL, w.HWND),
        "GetWindowThreadProcessId": sig(user32.GetWindowThreadProcessId, w.DWORD, w.HWND, P(w.DWORD)),
        "EnumWindows": sig(user32.EnumWindows, w.BOOL, WNDENUMPROC, w.LPARAM),
        "GetClassNameW": sig(user32.GetClassNameW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int),
        "GetWindowTextW": sig(user32.GetWindowTextW, ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int),
        "EnumDisplayMonitors": sig(user32.EnumDisplayMonitors, w.BOOL, w.HDC, P(w.RECT), MONITORENUMPROC, w.LPARAM),
        "GetMonitorInfoW": sig(user32.GetMonitorInfoW, w.BOOL, w.HMONITOR, P(MONITORINFO)),
        "CreatePopupMenu": sig(user32.CreatePopupMenu, w.HMENU),
        "AppendMenuW": sig(user32.AppendMenuW, w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR),
        "SetMenuDefaultItem": sig(user32.SetMenuDefaultItem, w.BOOL, w.HMENU, w.UINT, w.UINT),
        "TrackPopupMenu": sig(
            user32.TrackPopupMenu, ctypes.c_int, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID
        ),
        "DestroyMenu": sig(user32.DestroyMenu, w.BOOL, w.HMENU),
        "GetCursorPos": sig(user32.GetCursorPos, w.BOOL, P(w.POINT)),
        "GetSystemMetrics": sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int),
        "CreateIconIndirect": sig(user32.CreateIconIndirect, w.HICON, P(ICONINFO)),
        "DestroyIcon": sig(user32.DestroyIcon, w.BOOL, w.HICON),
        "MessageBeep": sig(user32.MessageBeep, w.BOOL, w.UINT),
        "Shell_NotifyIconW": sig(shell32.Shell_NotifyIconW, w.BOOL, w.DWORD, P(NOTIFYICONDATAW)),
        "CreateDIBSection": sig(
            gdi32.CreateDIBSection, w.HBITMAP, w.HDC, P(BITMAPINFOHEADER), w.UINT, P(w.LPVOID), w.HANDLE, w.DWORD
        ),
        "CreateBitmap": sig(gdi32.CreateBitmap, w.HBITMAP, ctypes.c_int, ctypes.c_int, w.UINT, w.UINT, w.LPVOID),
        "DeleteObject": sig(gdi32.DeleteObject, w.BOOL, w.HANDLE),
        "GetModuleHandleW": sig(kernel32.GetModuleHandleW, w.HMODULE, w.LPCWSTR),
        "CreateMutexW": sig(kernel32.CreateMutexW, w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR),
        "OpenProcess": sig(kernel32.OpenProcess, w.HANDLE, w.DWORD, w.BOOL, w.DWORD),
        "QueryFullProcessImageNameW": sig(kernel32.QueryFullProcessImageNameW, w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, P(w.DWORD)),
        "CloseHandle": sig(kernel32.CloseHandle, w.BOOL, w.HANDLE),
        "CreateJobObjectW": sig(kernel32.CreateJobObjectW, w.HANDLE, w.LPVOID, w.LPCWSTR),
        "SetInformationJobObject": sig(kernel32.SetInformationJobObject, w.BOOL, w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD),
        "AssignProcessToJobObject": sig(kernel32.AssignProcessToJobObject, w.BOOL, w.HANDLE, w.HANDLE),
    }
    try:  # real pixels, so the page can be placed exactly on the second screen
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor aware v2
    except (AttributeError, OSError):
        pass
    types = {
        "WNDPROC": WNDPROC,
        "WINEVENTPROC": WINEVENTPROC,
        "WNDENUMPROC": WNDENUMPROC,
        "MONITORENUMPROC": MONITORENUMPROC,
        "WNDCLASSEXW": WNDCLASSEXW,
        "NOTIFYICONDATAW": NOTIFYICONDATAW,
        "MONITORINFO": MONITORINFO,
        "BITMAPINFOHEADER": BITMAPINFOHEADER,
        "ICONINFO": ICONINFO,
        "JOBOBJECT_EXTENDED_LIMIT_INFORMATION": JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    }
    return type("Win32", (), {**api, **types, "ctypes": ctypes, "w": w})


class Helper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.win = _win32()
        self.status = STATUS_STARTING
        self.proc: subprocess.Popen | None = None
        self.owns_server = False
        self.stopping = False
        self.hotkey_on = False
        self.hotkey_failed = False
        self.vk = 0
        self.page_wanted = False  # open the page once the game no longer has the focus
        self.hold = Hold(self._send)
        self.messenger = Messenger(cfg.api, lambda: self._post(WM_UNHEARD))
        self.job = self._kill_on_close_job()
        self.icon = None
        self.hwnd = None
        self.taskbar_created = self.win.RegisterWindowMessageW("TaskbarCreated")  # sent when Explorer restarts

    # -- the hidden window, the tray icon and the menu ----------------------------------------

    def run(self) -> int:
        win = self.win
        hinstance = win.GetModuleHandleW(None)
        self._wndproc = win.WNDPROC(self._on_message)  # kept: Windows calls it for as long as we run
        wc = win.WNDCLASSEXW(cbSize=win.ctypes.sizeof(win.WNDCLASSEXW), lpfnWndProc=self._wndproc, hInstance=hinstance)
        wc.lpszClassName = WINDOW_CLASS
        win.RegisterClassExW(win.ctypes.byref(wc))
        # A plain hidden window rather than a message-only one: the tray menu needs a window that can come to the front.
        self.hwnd = win.CreateWindowExW(0, WINDOW_CLASS, NAME, 0, 0, 0, 0, 0, None, None, hinstance, None)
        self.icon = self._make_icon()
        self._tray(0)  # NIM_ADD
        self._on_foreground = win.WINEVENTPROC(lambda *args: self._update_hotkey())
        hook = win.SetWinEventHook(3, 3, None, self._on_foreground, 0, 0, 0x0002)  # EVENT_SYSTEM_FOREGROUND, skip own process
        self._update_hotkey()
        threading.Thread(target=self._run_server, name="server", daemon=True).start()

        msg = win.w.MSG()
        while win.GetMessageW(win.ctypes.byref(msg), None, 0, 0) > 0:
            win.TranslateMessage(win.ctypes.byref(msg))
            win.DispatchMessageW(win.ctypes.byref(msg))
        win.UnhookWinEvent(hook)
        return 0

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY and wparam == HOTKEY_ID:
                if self.hold.pressed():
                    self.win.SetTimer(self.hwnd, TIMER_ID, POLL_MS, None)
                return 0
            if msg == WM_TIMER and wparam == TIMER_ID:
                if self.hold.tick(self._key_down()):
                    self.win.KillTimer(self.hwnd, TIMER_ID)
                return 0
            if msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                return 0
            if msg == WM_SERVER:
                self._server_changed(wparam)
                return 0
            if msg == WM_UNHEARD:
                # ` in the game, but the page isn't open (or the server isn't up): a beep, never a window over the game.
                self.win.MessageBeep(0x30)
                if self.status == STATUS_READY:
                    self.page_wanted = True
                return 0
            if msg == WM_OPEN_PAGE:
                self.open_page()
                return 0
            if msg == WM_QUIT_HELPER:
                self.turn_off()
                return 0
            if msg == self.taskbar_created:  # Explorer restarted: put the icon back
                self._tray(0)
                return 0
            if msg == WM_DESTROY:
                self.win.PostQuitMessage(0)
                return 0
        except Exception:
            traceback.print_exc()
        return self.win.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _post(self, msg: int, wparam: int = 0) -> None:
        self.win.PostMessageW(self.hwnd, msg, wparam, 0)

    def _make_icon(self):
        win = self.win
        size = win.GetSystemMetrics(49) or 16  # SM_CXSMICON: the tray icon size at this screen's scaling
        header = win.BITMAPINFOHEADER(
            biSize=win.ctypes.sizeof(win.BITMAPINFOHEADER), biWidth=size, biHeight=-size, biPlanes=1, biBitCount=32
        )
        bits = win.w.LPVOID()
        colour = win.CreateDIBSection(None, win.ctypes.byref(header), 0, win.ctypes.byref(bits), None, 0)
        pixels = icon_pixels(size)
        win.ctypes.memmove(bits, pixels, len(pixels))
        mask_bits = win.ctypes.create_string_buffer((size + 15) // 16 * 2 * size)  # all clear: the alpha channel decides
        mask = win.CreateBitmap(size, size, 1, 1, mask_bits)
        info = win.ICONINFO(fIcon=True, hbmMask=mask, hbmColor=colour)
        icon = win.CreateIconIndirect(win.ctypes.byref(info))
        win.DeleteObject(colour)
        win.DeleteObject(mask)
        return icon

    def _tray(self, action: int, balloon: str = "", warning: bool = False) -> None:
        """NIM_ADD (0), NIM_MODIFY (1) or NIM_DELETE (2) the icon by the clock, optionally with a notification."""
        win = self.win
        data = win.NOTIFYICONDATAW(cbSize=win.ctypes.sizeof(win.NOTIFYICONDATAW), hWnd=self.hwnd, uID=1)
        data.uFlags = 0x1 | 0x2 | 0x4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self.icon
        data.szTip = self.status
        if balloon:
            data.uFlags |= 0x10  # NIF_INFO
            data.szInfoTitle = NAME
            data.szInfo = balloon
            data.dwInfoFlags = 0x2 if warning else 0x1  # NIIF_WARNING or NIIF_INFO
        win.Shell_NotifyIconW(action, win.ctypes.byref(data))

    def _set_status(self, status: str, balloon: str = "", warning: bool = False) -> None:
        self.status = status
        self._tray(1, balloon, warning)

    def _menu(self) -> None:
        win = self.win
        menu = win.CreatePopupMenu()
        win.AppendMenuW(menu, 0x1, 0, self.status)  # MF_GRAYED: a title line saying how things are
        win.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
        win.AppendMenuW(menu, 0, MENU_OPEN, "Open the answer page")
        win.AppendMenuW(menu, 0x8 if starts_at_login() else 0, MENU_STARTUP, "Start when I log in")  # MF_CHECKED
        win.AppendMenuW(menu, 0x800, 0, None)
        win.AppendMenuW(menu, 0, MENU_OFF, "Turn off Civspeak")
        win.SetMenuDefaultItem(menu, MENU_OPEN, 0)
        point = win.w.POINT()
        win.GetCursorPos(win.ctypes.byref(point))
        win.SetForegroundWindow(self.hwnd)  # so the menu closes when you click elsewhere
        # TPM_RETURNCMD | TPM_NONOTIFY | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON: open above the pointer, like other tray menus.
        choice = win.TrackPopupMenu(menu, 0x100 | 0x80 | 0x20 | 0x2, point.x, point.y, 0, self.hwnd, None)
        win.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        win.DestroyMenu(menu)
        if choice == MENU_OPEN:
            self.open_page()
        elif choice == MENU_STARTUP:
            set_start_at_login(not starts_at_login())
        elif choice == MENU_OFF:
            self.turn_off()

    # -- the ` key ---------------------------------------------------------------------------

    def _talk_vk(self) -> int:
        return self.win.MapVirtualKeyW(TALK_SCAN_CODE, 1) or 0xC0  # MAPVK_VSC_TO_VK, under the current layout

    def _key_down(self) -> bool:
        return bool(self.win.GetAsyncKeyState(self.vk) & 0x8000)

    def _send(self, key: str) -> None:
        self.messenger.send(key)

    def _update_hotkey(self) -> None:
        """Take ` while the game has the focus, and give it back when anything else does."""
        try:
            foreground = self.win.GetForegroundWindow()
            in_game = is_game(self._exe_of(foreground), self.cfg.games)
            if in_game and not self.hotkey_on:
                self.vk = self._talk_vk()
                # MOD_NOREPEAT: one message per press, however long the key is held.
                self.hotkey_on = bool(self.win.RegisterHotKey(self.hwnd, HOTKEY_ID, 0x4000, self.vk))
                if not self.hotkey_on and not self.hotkey_failed:
                    self.hotkey_failed = True
                    taken = "Another program is using the ` key, so Civspeak can't use it in the game."
                    self._set_status(self.status, taken, warning=True)
            elif not in_game and self.hotkey_on:
                self.win.UnregisterHotKey(self.hwnd, HOTKEY_ID)
                self.hotkey_on = False
            if not in_game and self.page_wanted:
                self.page_wanted = False
                self.open_page()
        except Exception:
            traceback.print_exc()

    def _exe_of(self, hwnd) -> str:
        win = self.win
        if not hwnd:
            return ""
        pid = win.w.DWORD()
        win.GetWindowThreadProcessId(hwnd, win.ctypes.byref(pid))
        process = win.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return ""
        try:
            buf = win.ctypes.create_unicode_buffer(1024)
            size = win.w.DWORD(len(buf))
            return buf.value if win.QueryFullProcessImageNameW(process, 0, buf, win.ctypes.byref(size)) else ""
        finally:
            win.CloseHandle(process)

    # -- the page ----------------------------------------------------------------------------

    def page_windows(self) -> list:
        """Brave windows showing the Civspeak page as an app (its window title is just the page title)."""
        win = self.win
        found = []

        def check(hwnd, _):
            if not win.IsWindowVisible(hwnd) or self._text(win.GetWindowTextW, hwnd) != PAGE_TITLE:
                return True
            if self._text(win.GetClassNameW, hwnd) == "Chrome_WidgetWin_1" and self._exe_of(hwnd).lower().endswith("brave.exe"):
                found.append(hwnd)
            return True

        win.EnumWindows(win.WNDENUMPROC(check), 0)
        return found

    def _text(self, fn, hwnd) -> str:
        buf = self.win.ctypes.create_unicode_buffer(256)
        fn(hwnd, buf, len(buf))
        return buf.value

    def screens(self) -> list:
        win = self.win
        found = []

        def add(monitor, _dc, _rect, _data):
            info = win.MONITORINFO(cbSize=win.ctypes.sizeof(win.MONITORINFO))
            if win.GetMonitorInfoW(monitor, win.ctypes.byref(info)):
                r = info.rcWork
                found.append(((r.left, r.top, r.right, r.bottom), bool(info.dwFlags & 1)))  # MONITORINFOF_PRIMARY
            return True

        win.EnumDisplayMonitors(None, None, win.MONITORENUMPROC(add), 0)
        return found

    def open_page(self) -> None:
        """Bring the page to the front, or open it filling the second screen."""
        windows = self.page_windows()
        if windows:
            if self.win.IsIconic(windows[0]):
                self.win.ShowWindow(windows[0], 9)  # SW_RESTORE
            self.win.SetForegroundWindow(windows[0])
        elif self.cfg.brave and os.path.exists(self.cfg.brave):
            subprocess.Popen(brave_command(self.cfg, pick_screen(self.screens())), close_fds=True)
        else:
            os.startfile(self.cfg.url)

    def close_page(self) -> None:
        for hwnd in self.page_windows():
            self.win.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    # -- the server in WSL -------------------------------------------------------------------

    def _kill_on_close_job(self):
        """A job that ends the server's wsl.exe (and with it the server) if this helper ever dies."""
        win = self.win
        job = win.CreateJobObjectW(None, None)
        info = win.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win.SetInformationJobObject(job, 9, win.ctypes.byref(info), win.ctypes.sizeof(info))
        return job

    def _run_server(self) -> None:
        try:
            if server_alive(self.cfg.api):  # already running (for example `civspeak serve` in a terminal)
                self._post(WM_SERVER, SERVER_READY)
                return
            log = open(self.cfg.folder / "server.log", "wb")  # wsl.exe writes to it for as long as it runs
            self.proc = subprocess.Popen(
                server_command(self.cfg),
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            self.owns_server = True
            process = self.win.OpenProcess(0x0101, False, self.proc.pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
            if process:
                self.win.AssignProcessToJobObject(self.job, process)
                self.win.CloseHandle(process)
            deadline = time.monotonic() + SERVER_START_SECONDS
            while not server_alive(self.cfg.api):
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    self._post(WM_SERVER, SERVER_FAILED)
                    return
                time.sleep(0.5)
            self._post(WM_SERVER, SERVER_READY)
            self.proc.wait()
            if not self.stopping:
                self._post(WM_SERVER, SERVER_STOPPED)
        except Exception:
            traceback.print_exc()
            self._post(WM_SERVER, SERVER_FAILED)

    def _server_changed(self, what: int) -> None:
        if self.stopping:
            return
        if what == SERVER_READY:
            self._set_status(STATUS_READY)
            if not self.page_windows():
                # Never put a window over the game: wait until it no longer has the focus.
                if self.hotkey_on:
                    self.page_wanted = True
                else:
                    self.open_page()
        else:
            message = "Civspeak could not start." if what == SERVER_FAILED else "Civspeak stopped."
            self._set_status(STATUS_STOPPED, f"{message} What went wrong is in {self.cfg.folder / 'server.log'}", True)

    # -- turning off -------------------------------------------------------------------------

    def turn_off(self) -> None:
        self.stopping = True
        if self.hotkey_on:
            self.win.UnregisterHotKey(self.hwnd, HOTKEY_ID)
        self._tray(2)  # NIM_DELETE
        if self.owns_server:
            self.close_page()
            if self.proc and self.proc.poll() is None:
                self.proc.stdin.close()  # the server stops when its input closes
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.win.DestroyWindow(self.hwnd)


# -- start at login, install, uninstall ------------------------------------------------------


def _run_command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{Path(__file__).resolve()}"'


def starts_at_login() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, NAME)
            return True
    except OSError:
        return False


def set_start_at_login(on: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, _run_command())
        else:
            try:
                winreg.DeleteValue(key, NAME)
            except FileNotFoundError:
                pass


def _running_helper():
    return _win32().FindWindowW(WINDOW_CLASS, None)


# Creates or deletes the Civspeak shortcuts in the Start menu and on the desktop (paths come in through
# the environment, so nothing needs quoting). Opening a shortcut starts Civspeak, or shows its page.
SHORTCUTS_SCRIPT = r"""
$places = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))
foreach ($place in $places) {
  $link = Join-Path $place 'Civspeak.lnk'
  if ($env:CIVSPEAK_REMOVE) { Remove-Item -LiteralPath $link -ErrorAction SilentlyContinue; continue }
  $s = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
  $s.TargetPath = $env:CIVSPEAK_PYTHONW
  $s.Arguments = '"' + $env:CIVSPEAK_SCRIPT + '"'
  $s.WorkingDirectory = $env:CIVSPEAK_FOLDER
  $s.IconLocation = $env:CIVSPEAK_ICON + ',0'
  $s.Description = 'Ask the Civilopedia out loud: hold ` in Civ VI'
  $s.Save()
}
"""


def set_shortcuts(on: bool) -> None:
    script = Path(__file__).resolve()
    icon = script.with_name("civspeak.ico")
    if on:
        icon.write_bytes(ico_file())
    env = {
        **os.environ,
        "CIVSPEAK_PYTHONW": str(Path(sys.executable).with_name("pythonw.exe")),
        "CIVSPEAK_SCRIPT": str(script),
        "CIVSPEAK_FOLDER": str(script.parent),
        "CIVSPEAK_ICON": str(icon),
        "CIVSPEAK_REMOVE": "" if on else "1",
    }
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", SHORTCUTS_SCRIPT]
    subprocess.run(command, env=env, check=True, capture_output=True, creationflags=0x08000000)


def install() -> int:
    """Shortcuts, start at every login, and start now (or, if it is already running, show the page)."""
    set_shortcuts(True)
    set_start_at_login(True)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:  # outlive whatever started the installer (WSL puts Windows programs it starts in a job)
        subprocess.Popen([str(pythonw), str(Path(__file__).resolve())], creationflags=flags | 0x01000000, close_fds=True)
    except OSError:
        subprocess.Popen([str(pythonw), str(Path(__file__).resolve())], creationflags=flags, close_fds=True)
    return 0


def stop() -> int:
    """Turn the running helper off (it closes the page and stops the server it started)."""
    win = _win32()
    hwnd = win.FindWindowW(WINDOW_CLASS, None)
    if hwnd:
        win.PostMessageW(hwnd, WM_QUIT_HELPER, 0, 0)
        deadline = time.monotonic() + 15
        while win.FindWindowW(WINDOW_CLASS, None) and time.monotonic() < deadline:
            time.sleep(0.2)
    return 0


def uninstall() -> int:
    """Remove the shortcuts, no longer start at login, and turn the running helper off."""
    set_shortcuts(False)
    set_start_at_login(False)
    return stop()


def main(argv: list[str]) -> int:
    folder = Path(__file__).resolve().parent
    command = argv[1] if len(argv) > 1 else ""
    if command in ("install", "uninstall", "stop"):
        return {"install": install, "uninstall": uninstall, "stop": stop}[command]()
    if command == "status":
        print(json.dumps({"running": bool(_running_helper()), "at_login": starts_at_login()}))
        return 0

    win = _win32()
    win.CreateMutexW(None, False, MUTEX_NAME)
    if win.ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: it is running; show its page instead
        hwnd = win.FindWindowW(WINDOW_CLASS, None)
        if hwnd:
            win.PostMessageW(hwnd, WM_OPEN_PAGE, 0, 0)
        return 0
    # pythonw has nowhere to print: problems go to helper.log beside this file.
    sys.stdout = sys.stderr = open(folder / "helper.log", "w", buffering=1, encoding="utf-8")
    try:
        return Helper(load_config(folder)).run()
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
