"""George's voice and the speech recognizer, fetched once on the Windows app's first run.

The installer stays small and updates never fetch them again: the model files live in the app's own
models folder (%LOCALAPPDATA%\\Civspeak\\models), which upgrades keep. They come from assets on
Civspeak's own GitHub Release (re-hosting is allowed: Kokoro is Apache-2.0, Whisper base.en MIT),
so a run never depends on Hugging Face or a third party being up. Each file's size and SHA-256 are
written here; a file that does not match is thrown away, never used.

The download runs on one background thread in the app's below normal process, resumes where it
stopped (a dropped connection, or the app turned off half way) and reports its progress for the
page. Files already in place are not fetched again.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import __version__, paths

log = logging.getLogger(__name__)

REPO = "OllySalanson/civspeak"
RELEASE_TAG = "models-v1"
DEFAULT_URL = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/"
CHUNK = 1 << 20
TIMEOUT_SECONDS = 30
ATTEMPTS = 4  # per file; each one resumes where the last stopped
RETRY_SECONDS = (2, 10, 30)
SPARE_BYTES = 50 << 20  # free space kept beyond the files themselves


@dataclass(frozen=True)
class Asset:
    """One model file: its name on the Release, where it goes in the models folder, its size and hash."""

    name: str
    path: str  # relative to the models folder, with /
    size: int
    sha256: str
    part_of: str  # what it is for, as the player would say it


KOKORO = "George's voice"
WHISPER = "speech recognition"

ASSETS: tuple[Asset, ...] = (
    # The int8 Kokoro: a third of the full model's size and memory, the same speed, and the same
    # words to Whisper's ear (measured on George, 22 Nov 2026: see paths.APP_KOKORO_MODEL).
    Asset(
        "kokoro-v1.0.int8.onnx",
        "kokoro-v1.0.int8.onnx",
        92361271,
        "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
        KOKORO,
    ),
    Asset(
        "voices-v1.0.bin",
        "voices-v1.0.bin",
        28214398,
        "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        KOKORO,
    ),
    Asset(
        "whisper-base.en-model.bin",
        "whisper-base.en/model.bin",
        145216508,
        "2a166925539a16005f14ff328359f9b9adb9dc4fb631bb3b227526862e93e2ef",
        WHISPER,
    ),
    Asset(
        "whisper-base.en-config.json",
        "whisper-base.en/config.json",
        2227,
        "f3bc3821e9fc76a27bae538e11ae5b677dcdd352b4600429ce7951d398569aeb",
        WHISPER,
    ),
    Asset(
        "whisper-base.en-tokenizer.json",
        "whisper-base.en/tokenizer.json",
        2128466,
        "929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df",
        WHISPER,
    ),
    Asset(
        "whisper-base.en-vocabulary.txt",
        "whisper-base.en/vocabulary.txt",
        422309,
        "ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf",
        WHISPER,
    ),
)


class DownloadError(RuntimeError):
    """The models could not be fetched; the message says why in the player's words."""


def base_url() -> str:
    """Where the files are fetched from (CIVSPEAK_MODELS_URL, for a mirror or a test, else the Release)."""
    url = os.environ.get("CIVSPEAK_MODELS_URL") or DEFAULT_URL
    return url if url.endswith("/") else url + "/"


def target(asset: Asset, folder: Path) -> Path:
    return folder.joinpath(*asset.path.split("/"))


def is_present(asset: Asset, folder: Path) -> bool:
    """In place and complete (its hash was checked when it was fetched, or it was put there by hand)."""
    try:
        return target(asset, folder).stat().st_size == asset.size
    except OSError:
        return False


def missing(folder: Path, assets: Iterable[Asset] = ASSETS) -> list[Asset]:
    return [a for a in assets if not is_present(a, folder)]


def mb(n: int) -> str:
    """A size as the player reads it: "92 MB", "2.1 MB", "0.4 MB"."""
    return f"{n / 1e6:.0f} MB" if n >= 10e6 else f"{n / 1e6:.1f} MB"


class Download:
    """Fetches the missing model files on one background thread; status() is safe to call any time."""

    def __init__(
        self,
        folder: Path | None = None,
        assets: Iterable[Asset] = ASSETS,
        url: str | None = None,
        sleep: Callable[[float], bool] | None = None,
    ):
        self.folder = folder or paths.models_dir()
        self.assets = tuple(assets)
        self.url = url or base_url()
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait  # returns True when stopped
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._done = 0  # bytes in place, counting the files already there
        self._file = ""
        self.error = ""
        self.finished = threading.Event()
        self.on_ready: list[Callable[[], None]] = []

    # -- what the page sees ---------------------------------------------------------------------

    @property
    def total(self) -> int:
        return sum(a.size for a in self.assets)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ready(self, part_of: str | None = None) -> bool:
        return not [a for a in missing(self.folder, self.assets) if part_of in (None, a.part_of)]

    def status(self) -> dict:
        """{"state": "ready" | "downloading" | "failed" | "missing", "done", "total", "file", "error"}."""
        with self._lock:
            done, file, error = self._done, self._file, self.error
        if self.running:
            state = "downloading"
        elif self.ready():
            state, done = "ready", self.total
        else:
            state = "failed" if error else "missing"
            if not error:
                done = self.total - sum(a.size for a in missing(self.folder, self.assets))
        return {"state": state, "done": done, "total": self.total, "file": file, "error": error}

    def problem(self, part_of: str) -> str:
        """Why `part_of` (KOKORO or WHISPER) cannot be used yet because of its files, or ""."""
        if self.ready(part_of):
            return ""
        s = self.status()
        if s["state"] == "downloading":
            percent = int(100 * s["done"] / s["total"]) if s["total"] else 0
            return f"Civspeak is downloading {part_of} ({percent}%)."
        if s["state"] == "failed":
            return f"Civspeak could not download {part_of}: {s['error']}"
        return f"Civspeak has not downloaded {part_of} yet."

    # -- running it -------------------------------------------------------------------------------

    def start(self) -> bool:
        """Start fetching whatever is missing; False if nothing is missing or it is already running."""
        with self._lock:
            if self.running or self.ready():
                return False
            self.error = ""
            self._stop.clear()
            self.finished.clear()
            self._thread = threading.Thread(target=self._run, name="model-download", daemon=True)
            self._thread.start()
        return True

    def stop(self, wait: float = 5) -> None:
        """Stop soon (between two chunks); what was fetched is kept and the next start resumes it."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(wait)

    def _run(self) -> None:
        try:
            self.fetch_all()
        except DownloadError as e:
            with self._lock:
                self.error = str(e)
            log.warning("model download failed: %s", e)
        except Exception as e:  # never let a surprise end the thread without saying so
            with self._lock:
                self.error = f"something went wrong ({e})"
            log.exception("model download failed")
        finally:
            with self._lock:
                self._file = ""
            self.finished.set()
        if self.ready():
            for callback in self.on_ready:
                try:
                    callback()
                except Exception:
                    log.exception("after the model download")

    def fetch_all(self) -> None:
        """Fetch every missing file in turn (in this thread), resuming any half-fetched one."""
        todo = missing(self.folder, self.assets)
        with self._lock:
            self._done = self.total - sum(a.size for a in todo)
        self._check_space(todo)
        for asset in todo:
            if self._stop.is_set():
                return
            with self._lock:
                self._file = asset.part_of
            self._fetch_retrying(asset)

    def _check_space(self, todo: list[Asset]) -> None:
        need = sum(a.size - _size(_part(target(a, self.folder))) for a in todo) + SPARE_BYTES
        self.folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.folder).free
        if free < need:
            raise DownloadError(f"there is not enough free disk space ({mb(need)} needed, {mb(free)} free on that drive).")

    def _fetch_retrying(self, asset: Asset) -> None:
        for attempt in range(ATTEMPTS):
            try:
                self._fetch(asset)
                return
            except DownloadError:
                raise
            except (OSError, urllib.error.URLError) as e:
                if attempt == ATTEMPTS - 1 or self._stop.is_set():
                    raise DownloadError(_network_problem(e)) from e
                log.info("model download of %s interrupted (%s); retrying", asset.name, e)
                if self._sleep(RETRY_SECONDS[min(attempt, len(RETRY_SECONDS) - 1)]):
                    return

    def _fetch(self, asset: Asset) -> None:
        dest = target(asset, self.folder)
        part = _part(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        have = _hash_into(digest, part, asset.size)
        base = self.total_before(asset)
        self._progress(base + have)
        if have < asset.size:
            request = urllib.request.Request(self.url + asset.name, headers={"User-Agent": f"Civspeak/{__version__}"})
            if have:
                request.add_header("Range", f"bytes={have}-")
            try:
                response = urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
            except urllib.error.HTTPError as e:
                if e.code == 416 and have:  # asked past the end: the part file is wrong, start it again
                    part.unlink(missing_ok=True)
                    raise ConnectionError("the partial file did not match") from e
                if e.code in (403, 404, 410):
                    raise DownloadError(f"the file {asset.name} is not on the download page (HTTP {e.code}).") from e
                raise
            with response:
                if have and response.status != 206:  # the server sent the whole file: start again
                    have, digest = 0, hashlib.sha256()
                with open(part, "ab" if have else "wb") as out:
                    while have < asset.size:
                        if self._stop.is_set():
                            return
                        chunk = response.read1(min(CHUNK, asset.size - have))  # what has arrived, up to CHUNK
                        if not chunk:
                            raise ConnectionError("the connection closed early")
                        out.write(chunk)
                        digest.update(chunk)
                        have += len(chunk)
                        self._progress(base + have)
        if digest.hexdigest() != asset.sha256 or _size(part) != asset.size:
            part.unlink(missing_ok=True)
            raise DownloadError(f"the file {asset.name} arrived damaged, so it was thrown away. Try again.")
        os.replace(part, dest)  # nothing has the new file open yet, so this works on Windows too
        log.info("fetched %s (%s)", asset.name, mb(asset.size))

    def total_before(self, asset: Asset) -> int:
        """Bytes counted as done before `asset`: every other file already in place."""
        return sum(a.size for a in self.assets if a is not asset and is_present(a, self.folder))

    def _progress(self, done: int) -> None:
        with self._lock:
            self._done = done


def _part(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _hash_into(digest, part: Path, size: int) -> int:
    """Feed a half-fetched file into the hash so the download can resume after it; its length, or 0
    if it is longer than the whole file (then it is thrown away)."""
    have = _size(part)
    if have > size:
        part.unlink(missing_ok=True)
        return 0
    if have:
        with open(part, "rb") as f:
            while block := f.read(CHUNK):
                digest.update(block)
    return have


def _network_problem(e: Exception) -> str:
    reason = getattr(e, "reason", None) or e
    return f"the internet connection failed ({reason}). Civspeak tries again the next time it starts."


def fetch(folder: Path | None = None, report: Callable[[dict], None] | None = None, every: float = 1.0) -> None:
    """Fetch the missing models and wait for them, reporting progress every second (`civspeak models fetch`)."""
    download = Download(folder)
    if not download.start():
        return
    while not download.finished.wait(every):
        if report:
            report(download.status())
    if download.error:
        raise DownloadError(download.error)
    if report:
        report(download.status())


def wait_ready(download: Download, timeout: float) -> bool:
    """For tests and the command line: True once the download thread has finished with every file in place."""
    deadline = time.monotonic() + timeout
    while download.running and time.monotonic() < deadline:
        download.finished.wait(0.05)
    return download.ready()
