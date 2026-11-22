"""Fetching George's voice and the speech recognizer on the Windows app's first run: a local server
stands in for the GitHub Release, serving invented bytes (with Range support, dropped connections
and servers that ignore Range), so nothing real is downloaded."""

import hashlib
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from civspeak import models, paths, server
from civspeak.answer import Answerer
from civspeak.hearing import Ears
from civspeak.speech import Voice


def asset(name: str, data: bytes, path: str | None = None, part_of: str = models.KOKORO) -> models.Asset:
    return models.Asset(name, path or name, len(data), hashlib.sha256(data).hexdigest(), part_of)


class Release:
    """What the fake download page serves, and what it was asked for."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.requests: list[tuple[str, str | None]] = []
        self.cut_after: dict[str, int] = {}  # name -> bytes sent before the connection drops (once)
        self.ignore_range = False
        self.wrong: set[str] = set()  # names served with damaged bytes
        self.gate: threading.Event | None = None  # when set, every response waits for it half way


@pytest.fixture
def release():
    rel = Release({})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            name = self.path.lstrip("/")
            rel.requests.append((name, self.headers.get("Range")))
            if name not in rel.files:
                self.send_error(404)
                return
            data = rel.files[name]
            if name in rel.wrong:
                data = bytes(255 - b for b in data)
            start = 0
            rng = self.headers.get("Range")
            if rng and not rel.ignore_range:
                start = int(rng.split("=")[1].rstrip("-"))
                if start >= len(data):
                    self.send_error(416)
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            else:
                self.send_response(200)
            body = data[start:]
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            cut = rel.cut_after.pop(name, None)
            if cut is not None:
                self.wfile.write(body[:cut])
                self.wfile.flush()
                self.close_connection = True
                return
            if rel.gate is not None:
                half = len(body) // 2
                self.wfile.write(body[:half])
                self.wfile.flush()
                rel.gate.wait(10)
                body = body[half:]
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    rel.url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    yield rel
    if rel.gate is not None:
        rel.gate.set()
    httpd.shutdown()
    httpd.server_close()


VOICE = bytes(range(256)) * 9000  # about 2.3 MB: several chunks
VOICES = b"george" * 1000
WHISPER = b"whisper weights " * 5000


@pytest.fixture
def assets(release):
    release.files = {"voice.onnx": VOICE, "voices.bin": VOICES, "whisper-model.bin": WHISPER}
    return (
        asset("voice.onnx", VOICE),
        asset("voices.bin", VOICES),
        asset("whisper-model.bin", WHISPER, "whisper/model.bin", models.WHISPER),
    )


def download(tmp_path, release, assets, **kw) -> models.Download:
    return models.Download(tmp_path / "models", assets, url=release.url, sleep=lambda s: False, **kw)


def test_everything_missing_is_fetched_checked_and_put_in_place(tmp_path, release, assets):
    d = download(tmp_path, release, assets)
    assert d.status() == {"state": "missing", "done": 0, "total": d.total, "file": "", "error": ""}
    assert d.problem(models.KOKORO) == "Civspeak has not downloaded George's voice yet."
    ready = []
    d.on_ready.append(lambda: ready.append(True))
    assert d.start()
    assert models.wait_ready(d, 10)
    assert (tmp_path / "models" / "voice.onnx").read_bytes() == VOICE
    assert (tmp_path / "models" / "whisper" / "model.bin").read_bytes() == WHISPER
    assert not list((tmp_path / "models").rglob("*.part"))
    assert d.status()["state"] == "ready" and d.status()["done"] == d.total
    assert d.problem(models.KOKORO) == d.problem(models.WHISPER) == ""
    assert ready == [True]
    assert not d.start()  # nothing left to do
    assert [r[1] for r in release.requests] == [None, None, None]


def test_files_already_in_place_are_not_fetched_again(tmp_path, release, assets):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "voice.onnx").write_bytes(VOICE)
    d = download(tmp_path, release, assets)
    assert d.status()["done"] == len(VOICE)
    assert not d.ready() and d.ready(models.WHISPER) is False
    d.start()
    assert models.wait_ready(d, 10)
    assert [r[0] for r in release.requests] == ["voices.bin", "whisper-model.bin"]


def test_a_half_fetched_file_resumes_where_it_stopped(tmp_path, release, assets):
    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "voice.onnx.part").write_bytes(VOICE[:1000])
    d = download(tmp_path, release, assets[:1])
    d.start()
    assert models.wait_ready(d, 10)
    assert release.requests == [("voice.onnx", "bytes=1000-")]
    assert (folder / "voice.onnx").read_bytes() == VOICE


def test_a_dropped_connection_is_retried_from_where_it_dropped(tmp_path, release, assets):
    release.cut_after["voice.onnx"] = 300_000
    d = download(tmp_path, release, assets[:1])
    d.start()
    assert models.wait_ready(d, 10)
    assert release.requests == [("voice.onnx", None), ("voice.onnx", "bytes=300000-")]
    assert (tmp_path / "models" / "voice.onnx").read_bytes() == VOICE


def test_a_server_that_ignores_resuming_sends_the_whole_file_again(tmp_path, release, assets):
    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "voice.onnx.part").write_bytes(VOICE[:1000])
    release.ignore_range = True
    d = download(tmp_path, release, assets[:1])
    d.start()
    assert models.wait_ready(d, 10)
    assert (folder / "voice.onnx").read_bytes() == VOICE


def test_a_part_file_longer_than_the_file_is_thrown_away(tmp_path, release, assets):
    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "voices.bin.part").write_bytes(VOICES + b"junk")
    d = download(tmp_path, release, assets[1:2])
    d.start()
    assert models.wait_ready(d, 10)
    assert release.requests == [("voices.bin", None)]
    assert (folder / "voices.bin").read_bytes() == VOICES


def test_a_damaged_file_is_thrown_away_and_never_used(tmp_path, release, assets):
    release.wrong.add("voices.bin")
    d = download(tmp_path, release, assets)
    d.start()
    assert not models.wait_ready(d, 10)
    s = d.status()
    assert s["state"] == "failed"
    assert s["error"] == "the file voices.bin arrived damaged, so it was thrown away. Try again."
    assert not (tmp_path / "models" / "voices.bin").exists()
    assert not (tmp_path / "models" / "voices.bin.part").exists()
    assert (tmp_path / "models" / "voice.onnx").exists()  # what did arrive whole is kept
    assert d.problem(models.KOKORO).startswith("Civspeak could not download George's voice: the file voices.bin")
    release.wrong.clear()
    assert d.start()  # trying again fetches only what is still missing
    assert models.wait_ready(d, 10)
    assert [r[0] for r in release.requests][-2:] == ["voices.bin", "whisper-model.bin"]


def test_a_missing_release_file_says_so(tmp_path, release, assets):
    del release.files["voices.bin"]
    d = download(tmp_path, release, assets)
    d.start()
    assert not models.wait_ready(d, 10)
    assert d.status()["error"] == "the file voices.bin is not on the download page (HTTP 404)."


def test_no_internet_gives_up_after_a_few_tries_and_says_why(tmp_path, assets):
    d = models.Download(tmp_path / "models", assets, url="http://127.0.0.1:9/", sleep=lambda s: False)
    d.start()
    assert not models.wait_ready(d, 30)
    error = d.status()["error"]
    assert error.startswith("the internet connection failed (") and error.endswith("tries again the next time it starts.")


def test_not_enough_disk_space_is_found_before_downloading(tmp_path, release, assets, monkeypatch):
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: type("U", (), {"free": 10_000_000})())
    d = download(tmp_path, release, assets)
    d.start()
    assert not models.wait_ready(d, 10)
    assert d.status()["error"] == "there is not enough free disk space (55 MB needed, 10 MB free on that drive)."
    assert release.requests == []


def test_progress_while_downloading_and_stopping_keeps_what_arrived(tmp_path, release, assets):
    release.gate = threading.Event()
    d = download(tmp_path, release, assets[:1])
    d.start()
    part = tmp_path / "models" / "voice.onnx.part"
    for _ in range(200):
        if d.status()["done"] >= len(VOICE) // 2:
            break
        d.finished.wait(0.02)
    s = d.status()
    assert s["state"] == "downloading" and s["file"] == models.KOKORO and s["done"] >= len(VOICE) // 2
    assert d.problem(models.KOKORO) == "Civspeak is downloading George's voice (50%)."
    d._stop.set()
    release.gate.set()
    d.stop()
    assert not d.running and not d.ready()
    kept = part.stat().st_size
    assert kept >= len(VOICE) // 2
    release.gate = None
    d.start()
    assert models.wait_ready(d, 10)
    assert release.requests[-1] == ("voice.onnx", f"bytes={kept}-")


def test_the_release_list_matches_where_the_app_looks(monkeypatch, tmp_path):
    names = [a.name for a in models.ASSETS]
    assert len(set(names)) == len(names) and not any("/" in n for n in names)
    assert all(len(a.sha256) == 64 and a.size > 0 for a in models.ASSETS)
    monkeypatch.setenv("CIVSPEAK_MODELS_DIR", str(tmp_path))
    for name in ("CIVSPEAK_KOKORO_DIR", "CIVSPEAK_WHISPER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    folder = paths.models_dir()
    targets = {models.target(a, folder) for a in models.ASSETS}
    model, voices = paths.kokoro_files()
    whisper = Path(paths.whisper_model())
    assert {model, voices, whisper / "model.bin"} <= targets
    assert {t.parent for t in targets} == {folder, whisper}
    assert models.DEFAULT_URL == "https://github.com/OllySalanson/civspeak/releases/download/models-v1/"
    monkeypatch.setenv("CIVSPEAK_MODELS_URL", "http://mirror.example/models")
    assert models.base_url() == "http://mirror.example/models/"


def test_the_app_fetches_its_models_and_loads_them_once_they_arrive(tmp_path, release, assets, monkeypatch):
    """The server's side: status shows the download, the voice and mic say why they wait, and a
    finished download wakes George and Whisper. POST /api/models tries again after a failure."""
    del release.files["voices.bin"]
    d = download(tmp_path, release, assets)
    state = server.State(
        tmp_path / "index", Answerer(), voice=Voice(), ears=Ears(model=str(tmp_path / "models" / "whisper")), download=d
    )
    state.check = lambda: None
    woken = []
    state.wake = lambda: woken.append(True)
    d.on_ready = [state.wake]
    assert state.voice.problem() == "Civspeak has not downloaded George's voice yet."
    assert state.ears.problem() == "Civspeak has not downloaded speech recognition yet."
    handler = type("H", (server.Handler,), {"state": state, "talk": server.Talk()})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        state.warm_up()
        state.unloader.stop()
        assert not models.wait_ready(d, 10)
        assert woken == [True]  # warm_up's own wake; the failed download wakes nothing
        woken.clear()
        status = json.loads(urllib.request.urlopen(f"{url}/api/status", timeout=5).read())
        assert status["models"]["state"] == "failed" and "voices.bin" in status["models"]["error"]
        assert status["voice"]["problem"].startswith("Civspeak could not download George's voice")
        release.files["voices.bin"] = VOICES
        req = urllib.request.Request(f"{url}/api/models", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        assert json.loads(urllib.request.urlopen(req, timeout=5).read())["state"] in ("downloading", "ready")
        assert models.wait_ready(d, 10)
        assert woken == [True]
        status = json.loads(urllib.request.urlopen(f"{url}/api/status", timeout=5).read())
        assert status["models"]["state"] == "ready"
        assert not status["mic"]["problem"].startswith("Civspeak")  # waiting on nothing (faster-whisper may be absent)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_without_the_windows_app_there_is_nothing_to_download(tmp_path, monkeypatch):
    monkeypatch.delenv("CIVSPEAK_MODELS_DIR", raising=False)
    state = server.State(tmp_path / "index", Answerer(), voice=Voice(), ears=Ears())
    assert state.download is None and state.voice.download is None
    assert state.status()["models"] is None
