"""The Windows app's updater: a local server stands in for GitHub's releases API and download
addresses, so nothing real is fetched or installed."""

import hashlib
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from civspeak import __version__, server, updates
from civspeak.answer import Answerer

SETUP = b"MZ pretend installer " * 5000


@pytest.fixture
def github():
    site = {"latest": None, "sums": "", "setup": SETUP, "hits": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            site["hits"].append(self.path)
            assert self.headers["User-Agent"] == f"Civspeak/{__version__}"
            if self.path == "/latest":
                if site["latest"] is None:
                    self.send_error(404)
                    return
                body = json.dumps(site["latest"]).encode()
            elif self.path == "/download/SHA256SUMS.txt":
                body = site["sums"].encode()
            elif self.path == "/download/Civspeak-Setup.exe":
                body = site["setup"]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def publish(version, setup=SETUP, sums=None, **extra):
        site["setup"] = setup
        site["sums"] = sums if sums is not None else f"{hashlib.sha256(setup).hexdigest()}  Civspeak-Setup.exe\n"
        site["latest"] = {
            "tag_name": f"v{version}",
            "html_url": f"https://example.invalid/releases/v{version}",
            "assets": [
                {"name": "Civspeak-Setup.exe", "browser_download_url": f"{base}/download/Civspeak-Setup.exe"},
                {"name": "SHA256SUMS.txt", "browser_download_url": f"{base}/download/SHA256SUMS.txt"},
            ],
            **extra,
        }

    site["publish"] = publish
    site["url"] = f"{base}/latest"
    yield site
    httpd.shutdown()
    httpd.server_close()


def newer(by=1):
    major, minor, patch = updates.version_key(__version__)
    return f"{major}.{minor}.{patch + by}"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def make(tmp_path, github, **kw):
    launched, turned_off = [], []
    u = updates.Updater(
        tmp_path,
        latest_url=github["url"],
        clock=kw.pop("clock", Clock()),
        launch=launched.append,
        turn_off=lambda: turned_off.append(1),
    )
    return u, launched, turned_off


def wait_until_not(u, *states, seconds=10):
    done = threading.Event()
    for _ in range(int(seconds / 0.02)):
        if u.status()["state"] not in states:
            return
        done.wait(0.02)
    raise AssertionError(f"still {u.status()['state']}")


def test_versions_compare_as_numbers():
    assert updates.version_key("v1.10.0") == (1, 10, 0) and updates.version_key("1.2.3") == (1, 2, 3)
    assert updates.version_key("v1.2.3-beta") is None and updates.version_key("latest") is None
    assert (
        updates.is_newer("v1.10.0", "1.9.9") and not updates.is_newer("1.2.3", "1.2.3") and not updates.is_newer("junk", "1.0.0")
    )


def test_a_release_needs_its_installer_and_checksums_and_must_be_published():
    assets = [
        {"name": "Civspeak-Setup.exe", "browser_download_url": "s"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "c"},
    ]
    assert updates.parse_release({"tag_name": "v2.0.0", "assets": assets}) == updates.Release("2.0.0", "s", "c", "")
    assert updates.parse_release({"tag_name": "v2.0.0", "assets": assets[:1]}) is None
    assert updates.parse_release({"tag_name": "v2.0.0", "assets": assets, "draft": True}) is None
    assert updates.parse_release({"tag_name": "v2.0.0", "assets": assets, "prerelease": True}) is None
    assert updates.parse_release({"tag_name": "nightly", "assets": assets}) is None


def test_checksum_lines():
    h = "ab" * 32
    assert updates.expected_hash(f"{h}  Civspeak-Setup.exe\n") == h
    assert updates.expected_hash(f"{h.upper()} *Civspeak-Setup.exe") == h
    assert updates.expected_hash(f"{h}  Other.exe") is None and updates.expected_hash("garbage") is None


def test_a_newer_release_is_found_once_a_day_and_told_once(tmp_path, github):
    clock = Clock()
    u, _, _ = make(tmp_path, github, clock=clock)
    told = []
    u.on_available.append(lambda r: told.append(r.version))
    assert u.check() is None and u.status()["state"] == "idle"  # nothing published yet (GitHub says 404)
    github["publish"](newer())
    assert u.check() is None  # already asked today
    clock.now += updates.CHECK_EVERY_SECONDS
    found = u.check()
    assert found.version == newer() and u.status()["state"] == "available" and told == [newer()]
    clock.now += updates.CHECK_EVERY_SECONDS
    u.check()
    assert told == [newer()]  # the same version is not announced again
    assert github["hits"].count("/latest") == 3
    # Remembered across restarts: a new Civspeak knows at once without asking GitHub.
    again, _, _ = make(tmp_path, github, clock=clock)
    assert again.status()["state"] == "available" and again.status()["version"] == newer()


def test_an_older_or_equal_release_is_not_offered(tmp_path, github):
    github["publish"](__version__)
    u, _, _ = make(tmp_path, github)
    assert u.check(force=True) is None and u.available is None


def test_installing_downloads_checks_starts_setup_and_turns_off(tmp_path, github):
    github["publish"](newer())
    u, launched, turned_off = make(tmp_path, github)
    u.check(force=True)
    assert u.install()
    wait_until_not(u, "downloading")
    setup = tmp_path / "updates" / f"Civspeak-Setup-{newer()}.exe"
    assert setup.read_bytes() == SETUP
    assert launched == [[str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"]]
    assert turned_off == [1] and u.status()["state"] == "installing"


def test_a_setup_that_does_not_match_its_checksum_is_never_run(tmp_path, github):
    github["publish"](newer(), sums=f"{'0' * 64}  Civspeak-Setup.exe\n")
    u, launched, turned_off = make(tmp_path, github)
    u.check(force=True)
    u.install()
    wait_until_not(u, "downloading")
    s = u.status()
    assert s["state"] == "failed" and "did not match its checksum" in s["error"]
    assert launched == [] and turned_off == [] and not list((tmp_path / "updates").iterdir())
    assert u.available is not None  # "Try again" is offered


def test_a_release_without_a_checksum_for_setup_is_refused(tmp_path, github):
    github["publish"](newer(), sums="")
    u, launched, _ = make(tmp_path, github)
    u.check(force=True)
    u.install()
    wait_until_not(u, "downloading")
    assert "does not list a checksum" in u.status()["error"] and launched == []


def test_offline_checks_quietly_try_again_tomorrow(tmp_path):
    u = updates.Updater(tmp_path, latest_url="http://127.0.0.1:9/latest", launch=pytest.fail)
    assert u.check(force=True) is None and u.status()["state"] == "idle" and u.status()["error"] == ""


def test_only_the_packaged_app_updates_itself(monkeypatch):
    monkeypatch.delenv("CIVSPEAK_UPDATE_URL", raising=False)
    monkeypatch.delattr("sys.frozen", raising=False)
    assert not updates.enabled()
    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert updates.enabled()


def test_settings_show_the_update_and_install_it(tmp_path, github):
    github["publish"](newer())
    u, launched, turned_off = make(tmp_path, github)
    state = server.State(tmp_path / "index", Answerer())
    state.check = lambda: None
    handler = type("H", (server.Handler,), {"state": state, "talk": server.Talk()})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(body):
        req = urllib.request.Request(
            f"{url}/api/update", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            post({})
        assert refused.value.code == 404  # running from source: no updater
        state.updater = u
        assert json.loads(urllib.request.urlopen(f"{url}/api/settings", timeout=5).read())["update"]["state"] == "idle"
        assert post({"check": True})["version"] == newer()
        post({})
        wait_until_not(u, "available", "downloading")
        assert launched and turned_off == [1]
    finally:
        httpd.shutdown()
        httpd.server_close()
