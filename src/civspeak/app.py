"""Civspeak as one Windows program: `Civspeak.exe` when packaged, `pythonw -m civspeak app` until then.

One process holds everything the WSL route spreads over three:

- the answer page's server, on a thread (server.make_server), at below normal priority like the
  rest of the process, so the game stays smooth;
- the icon by the clock, its menu and the ` key taken while Civ VI has the focus: winhelper's Helper
  on its own thread, which sleeps in GetMessage between key presses;
- the page itself in its own window (pywebview on WebView2, which Windows 10 and 11 already have),
  on the main thread as WinForms requires. Closing the window only hides it: the page keeps
  listening for ` and George keeps talking, and the tray menu or the Civspeak shortcut shows it
  again. "Turn off Civspeak" in the tray menu stops it all.

Only one Civspeak runs per Windows session (the same lock as the WSL helper, which also wants ` and
the port): starting a second one brings the running one's page to the front instead. "Start when I
log in" in the tray menu points the per-user Run key at this program.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from . import paths, server, system, winhelper

log = logging.getLogger(__name__)

LOG_NAME = "app.log"
ICON_NAME = "civspeak.ico"
WEBVIEW_FOLDER = "webview"  # WebView2's profile: the page's voice on/off setting and its cache
PAGE_BACKGROUND = "#0d1626"  # the page's own background (style.css --bg), so the window never flashes white
# The title bar in the page's header colours (style.css .top and --text) with light caption buttons:
# DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_CAPTION_COLOR and DWMWA_TEXT_COLOR (colours are 0x00BBGGRR).
TITLE_BAR = ((20, 1), (35, 0x0022130B), (36, 0x00F8F2EE))
# WebView2's browser switches: George may speak and the page may listen when ` is held in the game,
# without a click on the page first, and the page stays fully awake while the game covers or hides it.
BROWSER_ARGUMENTS = (
    "--disable-features=ElasticOverscroll",  # pywebview's own switch, which this setting replaces
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
)
CLOSE_REASON_USER = 3  # WinForms CloseReason.UserClosing: the window's X, Alt+F4 or the taskbar's Close
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
THREAD_PRIORITY_HIGHEST = 2
WINDOW_START_SECONDS = 60


def app_command() -> list[str]:
    """What starts the app: Civspeak.exe when packaged, otherwise this Python's pythonw running `civspeak app`."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [str(Path(sys.executable).with_name("pythonw.exe")), "-m", "civspeak", "app"]


def same_origin(uri: str, page_url: str) -> bool:
    """Whether `uri` is on the page's own server (only the page may use the microphone)."""
    a, b = urlsplit(uri), urlsplit(page_url)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)


class App(winhelper.Helper):
    log_name = LOG_NAME

    def __init__(self, httpd: server.Server, folder: Path, win=None):
        super().__init__(folder, win=win)
        self.httpd = httpd
        self.url = f"http://localhost:{httpd.server_address[1]}"
        self.window = None  # the pywebview window; None when this PC cannot show one (then the default browser)
        self.form_hwnd = 0  # its window, once created
        self.attached = threading.Event()
        self.shown = False
        self.placed = False
        self.serving = False

    def run(self) -> int:
        # Normal priority within this below normal process: ` is answered at once even while the game is busy.
        self.win.SetThreadPriority(self.win.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)
        return super().run()

    def run_command(self) -> str:
        return subprocess.list2cmdline(app_command())

    def press(self, key: str) -> bool:
        if key == "down":
            self.httpd.state.wake()  # load George and Whisper again while the player is still asking
        return self.httpd.talk.press(key)

    # -- the server ----------------------------------------------------------------------------

    def serve(self) -> None:
        if self.window is not None:
            self.attached.wait(WINDOW_START_SECONDS)  # "ready" shows the page, so its window must exist
        self._post(winhelper.WM_SERVER, winhelper.SERVER_READY)  # already listening: make_server bound the port
        self.serving = True
        try:
            self.httpd.serve_forever()
        except Exception:
            traceback.print_exc()
        finally:
            self.serving = False
        if not self.stopping:
            self._post(winhelper.WM_SERVER, winhelper.SERVER_STOPPED)

    # -- the page window -----------------------------------------------------------------------

    def attach(self, form) -> None:
        """The window's form was just created (on its own thread, before it is shown or the page loads)."""
        self.form_hwnd = form.Handle.ToInt64()
        form.FormClosing += self._on_closing
        form.webview.CoreWebView2InitializationCompleted += self._on_webview_ready
        self._style_title_bar()
        self._watch_theme()
        self.attached.set()

    def _style_title_bar(self) -> None:
        import ctypes

        for attribute, value in TITLE_BAR:
            v = ctypes.c_int(value)
            self.win.DwmSetWindowAttribute(self.form_hwnd, attribute, ctypes.byref(v), ctypes.sizeof(v))

    def _watch_theme(self) -> None:
        # pywebview gives the title bar Windows' light or dark look whenever the theme changes: put ours back after it.
        from Microsoft.Win32 import SystemEvents

        SystemEvents.UserPreferenceChanged += lambda sender, args: self._style_title_bar()

    def _on_closing(self, form, args) -> None:
        """Closing the window hides it, so the page keeps working; Windows shutting down or Civspeak
        turning off really closes it."""
        if self.stopping or int(args.CloseReason) != CLOSE_REASON_USER:
            return
        args.Cancel = True
        self.shown = False
        form.Hide()

    def _on_webview_ready(self, webview, args) -> None:
        if args.IsSuccess:
            webview.CoreWebView2.PermissionRequested += self._on_permission

    def _on_permission(self, core, args) -> None:
        # The page asks for the microphone; nothing else, and no other page, is granted anything.
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind, CoreWebView2PermissionState

        if args.PermissionKind == CoreWebView2PermissionKind.Microphone and same_origin(args.Uri, self.url):
            args.State = CoreWebView2PermissionState.Allow
        else:
            args.State = CoreWebView2PermissionState.Deny

    def page_open(self) -> bool:
        return self.shown

    def open_page(self) -> None:
        if self.window is None:
            os.startfile(self.url)  # no WebView2 on this PC: the player's default browser
            return
        win = self.win
        if not self.form_hwnd:
            return  # it could not be created (app.log says why)
        if not self.placed:  # the first time, fill the second screen (the main one if there is only one)
            left, top, right, bottom = winhelper.pick_screen(self.screens())
            win.SetWindowPos(self.form_hwnd, None, left, top, right - left, bottom - top, SWP_NOZORDER | SWP_NOACTIVATE)
            self.window.maximize()  # on that screen, flush with its edges; restoring it leaves it the same size there
            self.placed = True
        self.window.show()
        if win.IsIconic(self.form_hwnd):
            win.ShowWindow(self.form_hwnd, 9)  # SW_RESTORE
        win.SetForegroundWindow(self.form_hwnd)
        self.shown = True

    # -- turning off -------------------------------------------------------------------------

    def shut_down(self) -> None:
        if self.window is not None:
            self.window.destroy()  # the main thread's webview.start() returns
        if self.serving:
            self.httpd.shutdown()
        self.httpd.server_close()


def _webview():
    """pywebview, when it can show the page with WebView2 here; None otherwise."""
    try:
        import webview
        from webview.platforms import winforms
    except Exception:
        log.exception("The page window cannot be shown (pywebview or .NET is missing)")
        return None
    if winforms.renderer != "edgechromium":
        log.error("The page window cannot be shown: the WebView2 runtime is not installed")
        return None
    return webview


def _log_to(path: Path) -> None:
    """pythonw and the packaged app have no console: problems go to app.log instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    sys.stdout = sys.stderr = open(path, "w", buffering=1, encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.getLogger().level, format="%(asctime)s %(name)s: %(message)s", force=True)


def _icon(folder: Path) -> Path:
    icon = folder / ICON_NAME
    if not icon.exists():
        icon.write_bytes(winhelper.ico_file())
    return icon


def run(port: int = 8765) -> int:
    if not system.IS_WINDOWS:
        raise paths.SetupError("The Civspeak app runs under Windows' own Python. In WSL, use `civspeak helper install`.")
    # Also the first Windows call, which makes this process per-monitor DPI aware: real pixels everywhere.
    if not winhelper.claim_single_instance():
        return 0  # already running: its page was brought to the front instead
    folder = paths.data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _log_to(folder / LOG_NAME)
    # Its own taskbar button and icon, not Python's.
    import ctypes

    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(winhelper.NAME)
    system.lower_priority()

    httpd = server.make_server(port, any_port=True)
    app = App(httpd, folder)
    webview = _webview()
    if webview is not None:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = " ".join(BROWSER_ARGUMENTS)
        left, top, right, bottom = winhelper.pick_screen(app.screens())
        app.window = webview.create_window(
            winhelper.NAME,
            app.url,
            x=left,
            y=top,
            width=right - left,
            height=bottom - top,
            hidden=True,  # shown once the server is up and the game does not have the focus
            background_color=PAGE_BACKGROUND,
            text_select=True,
            zoomable=True,
        )
        app.window.events.before_show += lambda: app.attach(app.window.native)
    tray = threading.Thread(target=app.run, name="tray")
    tray.start()
    if webview is None:
        tray.join()
        return 0
    try:
        webview.start(
            gui="edgechromium",
            private_mode=False,
            storage_path=str(folder / WEBVIEW_FOLDER),
            icon=str(_icon(folder)),
        )
    except Exception:
        traceback.print_exc()
    if not app.stopping:  # the window went away by itself: turn everything off rather than run without it
        log.error("The page window closed unexpectedly; Civspeak is turning off")
        while app.hwnd is None and tray.is_alive():
            time.sleep(0.05)
        app._post(winhelper.WM_QUIT_HELPER)
    tray.join()
    return 0
