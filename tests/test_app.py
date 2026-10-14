"""The Windows app: one process with the server, the tray and ` key, and the page window.

The Windows halves (WinForms, WebView2, RegisterHotKey) only run on Windows; here they are fakes, so
what the app decides is tested: where the page opens, what closing it does, who may use the
microphone, how it starts and stops, and what Windows runs at login.
"""

import socket
import sys
import threading
import types

import pytest

from civspeak import app, cli, server, winhelper


class FakeWin:
    """Records the Windows calls the app makes."""

    def __init__(self, iconic=False):
        self.calls = []
        self.iconic = iconic

    def RegisterWindowMessageW(self, name):
        return 0xC123

    def IsIconic(self, hwnd):
        return self.iconic

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args: self.calls.append((name, *args)) or 1

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class FakeServer:
    def __init__(self, listening=True):
        self.server_address = ("127.0.0.1", 8799)
        self.woken = []
        self.keys = []
        self.state = types.SimpleNamespace(wake=lambda: self.woken.append(True))
        self.talk = types.SimpleNamespace(press=lambda key: self.keys.append(key) or listening)
        self.served = threading.Event()
        self.stopped = threading.Event()
        self.closed = False

    def serve_forever(self):
        self.served.set()
        self.stopped.wait(5)

    def shutdown(self):
        self.stopped.set()

    def server_close(self):
        self.closed = True


class FakeWindow:
    def __init__(self):
        self.calls = []

    def show(self):
        self.calls.append("show")

    def maximize(self):
        self.calls.append("maximize")

    def destroy(self):
        self.calls.append("destroy")


class Event:
    """A .NET event: handlers are added with +=."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeForm:
    def __init__(self):
        self.Handle = types.SimpleNamespace(ToInt64=lambda: 0x1234)
        self.FormClosing = Event()
        self.webview = types.SimpleNamespace(CoreWebView2InitializationCompleted=Event())
        self.hidden = False

    def Hide(self):
        self.hidden = True


MAIN = ((0, 0, 1920, 1032), True)
SECOND = ((0, 1080, 1920, 2112), False)


@pytest.fixture
def made(tmp_path, monkeypatch):
    """An app with a page window, its form attached, on a PC with two screens."""
    monkeypatch.setattr(app.App, "_watch_theme", lambda self: None)  # .NET's theme events
    win = FakeWin()
    a = app.App(FakeServer(), tmp_path, win=win)
    a.screens = lambda: [MAIN, SECOND]
    a._tray = lambda *args, **kwargs: None
    a.window = FakeWindow()
    form = FakeForm()
    a.attach(form)
    return a, win, form


def test_the_page_first_opens_filling_the_second_screen_then_stays_where_the_player_put_it(made):
    a, win, _ = made
    assert a.url == "http://localhost:8799" and not a.page_open()
    a.open_page()
    assert win.named("SetWindowPos") == [("SetWindowPos", 0x1234, None, 0, 1080, 1920, 1032, 0x0014)]
    assert a.window.calls == ["maximize", "show"] and win.named("SetForegroundWindow") == [("SetForegroundWindow", 0x1234)]
    assert a.page_open()

    win.iconic = True  # minimised since: restored, and not moved back
    a.open_page()
    assert len(win.named("SetWindowPos")) == 1 and a.window.calls == ["maximize", "show", "show"]
    assert win.named("ShowWindow") == [("ShowWindow", 0x1234, 9)]


def test_the_title_bar_wears_the_page_header_colours(made):
    _, win, _ = made
    styled = [(attr, size) for _, hwnd, attr, _value, size in win.named("DwmSetWindowAttribute") if hwnd == 0x1234]
    assert styled == [(20, 4), (35, 4), (36, 4)]


def test_closing_the_window_only_hides_it(made):
    a, _, form = made
    a.open_page()
    (on_closing,) = form.FormClosing.handlers
    close = types.SimpleNamespace(CloseReason=app.CLOSE_REASON_USER, Cancel=False)
    on_closing(form, close)
    assert close.Cancel and form.hidden and not a.page_open()

    shutdown = types.SimpleNamespace(CloseReason=1, Cancel=False)  # Windows is shutting down: let it close
    on_closing(form, shutdown)
    assert not shutdown.Cancel

    a.stopping = True  # "Turn off Civspeak"
    turn_off = types.SimpleNamespace(CloseReason=app.CLOSE_REASON_USER, Cancel=False)
    on_closing(form, turn_off)
    assert not turn_off.Cancel


def test_only_the_page_may_use_the_microphone(made, monkeypatch):
    a, _, form = made
    kinds = types.SimpleNamespace(Microphone="mic", Camera="camera")
    states = types.SimpleNamespace(Allow="allow", Deny="deny")
    core = types.ModuleType("Microsoft.Web.WebView2.Core")
    core.CoreWebView2PermissionKind, core.CoreWebView2PermissionState = kinds, states
    monkeypatch.setitem(sys.modules, "Microsoft.Web.WebView2.Core", core)

    webview = types.SimpleNamespace(CoreWebView2=types.SimpleNamespace(PermissionRequested=Event()))
    (on_ready,) = form.webview.CoreWebView2InitializationCompleted.handlers
    on_ready(webview, types.SimpleNamespace(IsSuccess=True))
    (on_permission,) = webview.CoreWebView2.PermissionRequested.handlers

    def asked(kind, uri):
        request = types.SimpleNamespace(PermissionKind=kind, Uri=uri, State="default")
        on_permission(None, request)
        return request.State

    assert asked("mic", "http://localhost:8799/") == "allow"
    assert asked("camera", "http://localhost:8799/") == "deny"
    assert asked("mic", "http://localhost:8800/") == "deny"
    assert asked("mic", "https://example.com/") == "deny"


def test_same_origin():
    assert app.same_origin("http://localhost:8765/?q=1", "http://localhost:8765")
    assert not app.same_origin("http://localhost.example.com:8765/", "http://localhost:8765")
    assert not app.same_origin("https://localhost:8765/", "http://localhost:8765")


def test_a_held_key_wakes_the_voice_and_goes_to_the_page_in_this_process(made):
    a, _, _ = made
    assert a.press("down") is True and a.press("up") is True
    assert a.httpd.keys == ["down", "up"] and a.httpd.woken == [True]


def test_ready_opens_the_page_but_never_over_the_game(made):
    a, _, _ = made
    a.hotkey_on = True  # Civ has the focus
    a._server_changed(winhelper.SERVER_READY)
    assert a.status == winhelper.STATUS_READY and a.window.calls == [] and a.page_wanted
    a.hotkey_on = a.page_wanted = False
    a._server_changed(winhelper.SERVER_READY)
    assert a.window.calls == ["maximize", "show"]


def test_serving_waits_for_the_window_and_turning_off_stops_everything(made):
    a, win, _ = made
    thread = threading.Thread(target=a.serve)
    thread.start()
    assert a.httpd.served.wait(5)
    assert win.named("PostMessageW") == [("PostMessageW", None, winhelper.WM_SERVER, winhelper.SERVER_READY, 0)]

    a.turn_off()
    thread.join(5)
    assert not thread.is_alive()
    assert a.window.calls == ["destroy"] and a.httpd.closed
    assert win.named("DestroyWindow") and len(win.named("PostMessageW")) == 1  # turned off: no "stopped" warning


def test_without_webview2_the_page_opens_in_the_default_browser(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(app.os, "startfile", opened.append, raising=False)
    a = app.App(FakeServer(), tmp_path, win=FakeWin())
    a.open_page()
    assert opened == ["http://localhost:8799"]


def test_windows_runs_the_app_at_login(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", "/venv/Scripts/python.exe")  # this Python's pythonw: no console window
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert app.app_command() == ["/venv/Scripts/pythonw.exe", "-m", "civspeak", "app"]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Users\Me\AppData\Local\Programs\Civspeak\Civspeak.exe")
    assert app.app_command() == [r"C:\Users\Me\AppData\Local\Programs\Civspeak\Civspeak.exe"]
    a = app.App(FakeServer(), tmp_path, win=FakeWin())
    assert a.run_command() == r"C:\Users\Me\AppData\Local\Programs\Civspeak\Civspeak.exe"


def test_civspeak_exe_on_its_own_is_the_app(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_app", lambda args: seen.append(args.port) or 0)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert cli.main([]) == 0
    assert seen == [8765]


def test_the_app_says_where_it_runs(capsys):
    assert cli.main(["app"]) == 2  # these tests run on Linux: the app is Windows only
    assert "Windows' own Python" in capsys.readouterr().err


@pytest.fixture
def quiet_server(monkeypatch):
    monkeypatch.setattr(server.State, "warm_up", lambda self: None)  # never load the real voice in a test


def test_the_server_takes_another_port_only_when_asked(quiet_server):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    try:
        with pytest.raises(OSError):
            server.make_server(port)
        httpd = server.make_server(port, any_port=True)
        try:
            assert httpd.server_address[1] not in (0, port)
            assert httpd.RequestHandlerClass.state is httpd.state and httpd.RequestHandlerClass.talk is httpd.talk
        finally:
            httpd.server_close()
    finally:
        taken.close()
