"""Keeping the Windows app up to date: a small updater, not a framework.

At most once a day the packaged app asks GitHub's public API for Civspeak's latest published release
(no login; drafts and pre-releases are never offered). When it is newer than this one, the tray and
the Settings screen say so. Installing it, which the player starts, downloads Civspeak-Setup.exe,
checks it against the SHA-256 the release lists in SHA256SUMS.txt (a file that does not match is
thrown away), starts it silently in update mode and turns Civspeak off. Setup waits for Civspeak to
close, replaces the program files, keeps the player's settings, key and models (they live in
%LOCALAPPDATA%\\Civspeak, not in the program folder) and starts the new Civspeak.

Only the packaged app updates itself; running from source never does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__, paths, system

log = logging.getLogger(__name__)

REPO = "OllySalanson/civspeak"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
SETUP_NAME = "Civspeak-Setup.exe"
SUMS_NAME = "SHA256SUMS.txt"
CHECK_EVERY_SECONDS = 24 * 3600
FIRST_CHECK_SECONDS = 60  # after starting: never while the player is busy launching the game
TIMEOUT_SECONDS = 30
STATE_FILE = "update.json"
# Setup's own switches: silent, no questions, wait for Civspeak to close, then start it again.
SETUP_ARGUMENTS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1")
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def version_key(text: str) -> tuple[int, int, int] | None:
    """(1, 2, 3) for "v1.2.3" or "1.2.3"; None for anything else (such as "v1.2.3-beta")."""
    m = _VERSION.match(text.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, now = version_key(candidate), version_key(current)
    return new is not None and now is not None and new > now


@dataclass
class Release:
    version: str
    setup_url: str
    sums_url: str
    page_url: str = ""


def parse_release(data: dict) -> Release | None:
    """The installer and checksum addresses of a GitHub release, or None if it lacks either (or is a
    draft or pre-release, which /releases/latest never returns anyway)."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    version = str(data.get("tag_name") or "")
    if version_key(version) is None:
        return None
    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets") or [] if isinstance(a, dict)}
    if not assets.get(SETUP_NAME) or not assets.get(SUMS_NAME):
        return None
    return Release(version.lstrip("v"), assets[SETUP_NAME], assets[SUMS_NAME], str(data.get("html_url") or ""))


def expected_hash(sums: str, name: str = SETUP_NAME) -> str | None:
    """The SHA-256 a SHA256SUMS.txt lists for `name` ("<hash>  <name>" or "<hash> *<name>" per line)."""
    for line in sums.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


class UpdateError(RuntimeError):
    """The update could not be fetched or checked; the message says why in the player's words."""


def _get(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": f"Civspeak/{__version__}", "Accept": "application/json"})


class Updater:
    """Checks for a newer release (once a day, on a background thread) and installs it on request.

    `launch` starts Setup (for tests); `turn_off` stops this Civspeak so Setup can replace it."""

    def __init__(
        self,
        folder: Path | None = None,
        latest_url: str | None = None,
        clock: Callable[[], float] = time.time,
        launch: Callable[[list[str]], None] | None = None,
        turn_off: Callable[[], None] | None = None,
    ):
        self.folder = folder or paths.data_dir()
        self.latest_url = latest_url or os.environ.get("CIVSPEAK_UPDATE_URL") or LATEST_URL
        self.clock = clock
        self.launch = launch or _launch
        self.turn_off = turn_off
        self.lock = threading.Lock()
        self.state = "idle"  # idle | checking | available | downloading | installing | failed
        self.error = ""
        self.release: Release | None = None
        self.on_available: list[Callable[[Release], None]] = []
        self._stop = threading.Event()
        saved = self._load()
        if saved.get("release") and is_newer(saved["release"].get("version", "")):
            self.release = Release(**saved["release"])
            self.state = "available"

    # -- what the page and the tray see ------------------------------------------------------------

    def status(self) -> dict:
        with self.lock:
            release = self.release
            return {
                "state": self.state,
                "current": __version__,
                "version": release.version if release else "",
                "page": release.page_url if release else "",
                "error": self.error,
                "checked_at": self._load().get("checked_at"),
            }

    @property
    def available(self) -> Release | None:
        with self.lock:
            return self.release if self.state in ("available", "failed") and self.release else None

    # -- checking ----------------------------------------------------------------------------------

    def start(self) -> None:
        """Check soon after starting, then once a day for as long as Civspeak runs."""
        threading.Thread(target=self._run, name="update-check", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._stop.wait(FIRST_CHECK_SECONDS):
            return
        while True:
            self.check()
            if self._stop.wait(3600):  # look again in an hour whether a day has gone by
                return

    def check(self, force: bool = False) -> Release | None:
        """Ask GitHub for the latest release unless that was done in the last day; the newer release, or None."""
        saved = self._load()
        if not force and self.clock() - float(saved.get("checked_at") or 0) < CHECK_EVERY_SECONDS:
            return self.available
        with self.lock:
            if self.state in ("downloading", "installing"):
                return self.release
            self.state, self.error = "checking", ""
        found = None
        try:
            with urllib.request.urlopen(_get(self.latest_url), timeout=TIMEOUT_SECONDS) as response:
                found = parse_release(json.loads(response.read(1_000_000)))
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404: no release published yet
                log.info("update check failed: HTTP %s", e.code)
        except (OSError, ValueError) as e:
            log.info("update check failed: %s", e)  # offline: try again tomorrow; nothing to tell the player
        newer = found if found and is_newer(found.version) else None
        self._save({"checked_at": self.clock(), "release": asdict(newer) if newer else None})
        with self.lock:
            told = self.release is not None and newer is not None and self.release.version == newer.version
            self.release = newer
            self.state = "available" if newer else "idle"
        if newer and not told:
            for callback in self.on_available:
                try:
                    callback(newer)
                except Exception:
                    log.exception("telling the player about the update")
        return newer

    # -- installing --------------------------------------------------------------------------------

    def install(self) -> bool:
        """Download, check and start the newer Setup on a background thread; False if there is none."""
        with self.lock:
            if self.release is None or self.state in ("downloading", "installing"):
                return False
            self.state, self.error = "downloading", ""
        threading.Thread(target=self._install, name="update-install", daemon=True).start()
        return True

    def _install(self) -> None:
        release = self.release
        try:
            setup = self.fetch(release)
            with self.lock:
                self.state = "installing"
            self.launch([str(setup), *SETUP_ARGUMENTS])
            log.info("started Setup for Civspeak %s; turning off so it can replace this one", release.version)
            if self.turn_off is not None:
                self.turn_off()
        except UpdateError as e:
            with self.lock:
                self.state, self.error = "failed", str(e)
        except Exception as e:  # never leave the page saying "downloading" for ever
            log.exception("update failed")
            with self.lock:
                self.state, self.error = "failed", f"something went wrong ({e})"

    def fetch(self, release: Release) -> Path:
        """Civspeak-Setup.exe for `release`, downloaded and checked against the release's SHA256SUMS.txt."""
        try:
            with urllib.request.urlopen(_get(release.sums_url), timeout=TIMEOUT_SECONDS) as response:
                want = expected_hash(response.read(100_000).decode("utf-8", "replace"))
        except (OSError, ValueError) as e:
            raise UpdateError(f"the update's checksum could not be downloaded ({e}).") from e
        if want is None:
            raise UpdateError(f"the update does not list a checksum for {SETUP_NAME}, so it was not installed.")
        folder = self.folder / "updates"
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("Civspeak-Setup-*"):
            old.unlink(missing_ok=True)  # an earlier update's installer, finished with
        target = folder / f"Civspeak-Setup-{release.version}.exe"
        part = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(_get(release.setup_url), timeout=TIMEOUT_SECONDS) as response, open(part, "wb") as out:
                while chunk := response.read(1 << 20):
                    if self._stop.is_set():
                        raise UpdateError("Civspeak was turned off during the download.")
                    out.write(chunk)
                    digest.update(chunk)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise UpdateError(f"the update could not be downloaded ({e}). Try again later.") from e
        if digest.hexdigest() != want:
            part.unlink(missing_ok=True)
            raise UpdateError("the downloaded update did not match its checksum, so it was thrown away. Try again later.")
        os.replace(part, target)
        return target

    # -- the once-a-day memory ---------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            data = json.loads((self.folder / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            temp = self.folder / (STATE_FILE + ".new")
            temp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(temp, self.folder / STATE_FILE)
        except OSError as e:
            log.info("could not remember the update check: %s", e)


def _launch(command: list[str]) -> None:
    """Start Setup on its own (it outlives this Civspeak), without a console window."""
    options = {"creationflags": 0x00000008 | 0x00000200} if system.IS_WINDOWS else {}  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen(command, close_fds=True, **options)


def enabled() -> bool:
    """Whether this Civspeak updates itself: only the packaged app (CIVSPEAK_UPDATE_URL turns it on for tests)."""
    return bool(getattr(sys, "frozen", False)) or bool(os.environ.get("CIVSPEAK_UPDATE_URL"))
