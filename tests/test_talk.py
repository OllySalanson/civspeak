"""Holding ` in a game: the Windows helper's logic, the server's talk channel, and the installer.

The helper's Windows half (RegisterHotKey, the tray icon) only runs on Windows; it is exercised by
tools/helper_check.py. Everything it decides is tested here.
"""

import http.client
import io
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from civspeak import helper, server, winhelper
from civspeak.answer import Answerer
from civspeak.spend import Ledger


@pytest.fixture
def talk_server(built_index, tmp_path):
    out, _ = built_index
    state = server.State(out.parent, Answerer(Ledger(tmp_path / "spend.jsonl"), tmp_path / "no-key"))
    state.check = lambda: None
    handler = type("H", (server.Handler,), {"state": state, "talk": server.Talk()})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


class Page:
    """Stands in for the answer page: holds /api/talk open and collects the talk events."""

    def __init__(self, port):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.conn.request("GET", "/api/talk")
        self.reply = self.conn.getresponse()
        assert self.reply.status == 200
        assert self.reply.getheader("Content-Type").startswith("text/event-stream")
        assert self.reply.fp.readline() == b"retry: 2000\n"

    def next_key(self):
        lines = []
        while True:
            line = self.reply.fp.readline().decode().rstrip("\n")
            if line:
                lines.append(line)
            elif lines and lines[0].startswith("event:"):
                break
        assert lines[0] == "event: talk"
        return json.loads(lines[1].removeprefix("data: "))["key"]

    def close(self):
        self.reply.close()
        self.conn.close()


def press(port, key):
    return winhelper.post_key(f"http://127.0.0.1:{port}", key)


def wait_for(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_a_held_key_reaches_the_newest_open_page_only(talk_server):
    assert press(talk_server, "down") is False  # no page open: the helper beeps instead
    older = Page(talk_server)
    newer = Page(talk_server)
    assert press(talk_server, "down") and press(talk_server, "up")
    assert newer.next_key() == "down" and newer.next_key() == "up"

    newer.close()  # a closed page is noticed at once, and the older one takes over
    time.sleep(0.1)
    assert press(talk_server, "down")
    assert older.next_key() == "down"
    older.close()
    time.sleep(0.1)
    assert press(talk_server, "up") is False


def test_hotkey_posts_must_be_json_with_a_known_key(talk_server):
    url = f"http://127.0.0.1:{talk_server}/api/hotkey"
    for body, ctype, code in [
        (b"key=down", "application/x-www-form-urlencoded", 415),  # what another website could send
        (b'{"key": "sideways"}', "application/json", 400),
    ]:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == code


def test_messenger_sends_in_order_and_reports_an_unheard_press(talk_server):
    unheard = []
    messenger = winhelper.Messenger(lambda key: press(talk_server, key), lambda: unheard.append(True))
    messenger.send("down")
    wait_for(lambda: unheard)
    page = Page(talk_server)
    for key in ("down", "up", "down", "up"):
        messenger.send(key)
    assert [page.next_key() for _ in range(4)] == ["down", "up", "down", "up"]
    assert len(unheard) == 1
    page.close()


def test_the_server_stops_when_the_helper_closes_its_input(monkeypatch):
    stopped = []
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"anything\n")))
    server._stop_when_stdin_closes(type("Httpd", (), {"shutdown": lambda self: stopped.append(True)})())
    assert stopped == [True]


def test_hold_sends_down_once_and_up_on_release():
    sent = []
    hold = winhelper.Hold(sent.append)
    assert hold.pressed() is True  # start watching for the release
    assert hold.pressed() is False  # a second hotkey message while held changes nothing
    assert hold.tick(still_down=True) is False
    assert hold.tick(still_down=True) is False
    assert hold.tick(still_down=False) is True  # released: stop the timer
    assert hold.tick(still_down=False) is True
    assert sent == ["down", "up"]
    assert hold.pressed() is True and sent == ["down", "up", "down"]


def test_only_the_game_gets_its_key_taken():
    games = list(winhelper.GAMES)
    exe = r"C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI\Base\Binaries\Win64Steam"
    assert winhelper.is_game(exe + r"\CivilizationVI_DX12.exe", games)
    assert winhelper.is_game(exe + r"\civilizationvi.exe", games)
    assert not winhelper.is_game(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe", games)
    assert not winhelper.is_game("", games)


def test_the_page_opens_filling_the_second_screen(tmp_path):
    main = ((0, 0, 1920, 1032), True)
    second = ((0, 1080, 1920, 2112), False)  # below the main screen, its taskbar excluded
    assert winhelper.pick_screen([main, second]) == (0, 1080, 1920, 2112)
    assert winhelper.pick_screen([main]) == (0, 0, 1920, 1032)

    (tmp_path / "config.json").write_text(
        json.dumps({"distro": "Ubuntu", "python": "/home/me/civspeak/.venv/bin/python", "brave": r"C:\b\brave.exe"})
    )
    cfg = winhelper.load_config(tmp_path)
    command = winhelper.brave_command(cfg, (0, 1080, 1920, 2112))
    assert command[:3] == [r"C:\b\brave.exe", f"--user-data-dir={tmp_path / 'brave-profile'}", "--app=http://localhost:8765"]
    assert "--window-position=0,1080" in command and "--window-size=1920,1032" in command
    assert "--autoplay-policy=no-user-gesture-required" in command


def test_the_server_runs_in_wsl_until_the_helper_lets_go(tmp_path):
    cfg = winhelper.Config(distro="Ubuntu", python="/home/me/civspeak/.venv/bin/python", port=8800, folder=tmp_path)
    assert winhelper.server_command(cfg) == [
        "wsl.exe", "-d", "Ubuntu", "--cd", "~", "-e",
        "/home/me/civspeak/.venv/bin/python", "-u", "-m", "civspeak", "serve", "--port", "8800", "--watch-stdin",
    ]  # fmt: skip
    assert cfg.api == "http://127.0.0.1:8800" and cfg.url == "http://localhost:8800"


def test_tray_icon_is_the_page_star():
    size = 16
    pixels = winhelper.icon_pixels(size)
    assert len(pixels) == size * size * 4

    def at(x, y):
        b, g, r, a = pixels[(y * size + x) * 4 : (y * size + x) * 4 + 4]
        return (r, g, b), a

    assert at(0, 0)[1] == 0  # rounded corner: transparent
    assert at(0, 8) == (winhelper.ICON_NAVY, 255)  # the navy square's edge
    assert at(8, 8) == (winhelper.ICON_GOLD, 255)  # the star's middle


def test_install_copies_the_helper_and_starts_it(tmp_path, monkeypatch):
    calls = []
    appdata = tmp_path / "AppData" / "Local"
    appdata.mkdir(parents=True)

    def windows(*cmd, timeout=60):
        calls.append(cmd)
        if cmd[0] == "cmd.exe":
            return type("Done", (), {"stdout": "C:\\Users\\me\\AppData\\Local\r\n", "returncode": 0, "stderr": ""})
        if "-c" in cmd:
            return type("Done", (), {"stdout": "True C:\\Python314\\python.exe\n", "returncode": 0, "stderr": ""})
        return type("Done", (), {"stdout": "", "returncode": 0, "stderr": ""})

    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(helper.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(helper, "_windows", windows)
    monkeypatch.setattr(helper, "_to_wsl", lambda p: appdata)
    monkeypatch.setattr(helper, "_to_windows", lambda p: "C:" + str(p).replace("/", "\\"))
    monkeypatch.setattr(helper, "BRAVE", [])
    message = helper.install(port=8800)

    folder = appdata / "Civspeak"
    script = folder / "civspeak_helper.pyw"
    assert script.read_text() == Path(winhelper.__file__).read_text()
    config = json.loads((folder / "config.json").read_text())
    assert config == {"distro": "Ubuntu", "python": sys.executable, "port": 8800, "brave": ""}
    assert calls[-1][0] == "C:\\Python314\\python.exe" and calls[-1][-1] == "install"
    assert "default browser" in message
    winhelper.load_config(folder)  # the helper can read what the installer wrote


def test_closed_detection_on_a_real_socket():
    a, b = socket.socketpair()
    assert server._closed(a) is False
    b.close()
    assert server._closed(a) is True
    a.close()


def test_shortcut_icon_file_holds_every_size():
    import struct

    data = winhelper.ico_file((16, 32, 256))
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind, count) == (0, 1, 3)
    for n, size in enumerate((16, 32, 256)):
        w, h, _, _, planes, bits, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * n)
        assert (w, h, planes, bits) == (size % 256, size % 256, 1, 32)
        assert struct.unpack_from("<Iii", data, offset) == (40, size, size * 2)
        assert offset + length <= len(data)
