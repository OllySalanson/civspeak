"""A small local web server for the answer page.

GET  /             the page
GET  /api/status   index age, entry count, key present, this month's spend
POST /api/ask      {"question": "..."} -> text/event-stream of:
                     entries  the matched Civilopedia entries (always sent first)
                     delta    answer text as it streams
                     done     final answer, entries used, cost and month total
                     problem  why there is no answer (no key, monthly limit, API error)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import index as index_mod
from . import paths, spend
from .answer import Answerer, AnswerError, KeyMissing
from .search import Index

log = logging.getLogger(__name__)

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
MAX_QUESTION = 500
STALE_CHECK_SECONDS = 10


class State:
    """The current index, plus a background rebuild when the game's cache or log changes."""

    def __init__(self, index_path: Path, answerer: Answerer):
        self.index_path = index_path
        self.answerer = answerer
        self.lock = threading.Lock()
        self.index: Index | None = None
        self.building = False
        self.build_error = ""
        self._last_check = 0.0
        if index_path.exists():
            self.index = Index(index_path)

    def check(self) -> None:
        now = time.monotonic()
        if self.building or now - self._last_check < STALE_CHECK_SECONDS and self.index is not None:
            return
        self._last_check = now
        try:
            game = paths.discover()
            stale = index_mod.is_stale(self.index_path, game)
        except paths.SetupError as e:
            self.build_error = str(e)
            return
        if stale:
            self.building = True
            threading.Thread(target=self._rebuild, daemon=True).start()

    def _rebuild(self) -> None:
        try:
            # A separate low-priority process, so the build never competes with the game.
            proc = subprocess.run(
                [sys.executable, "-m", "civspeak", "build"],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if proc.returncode != 0:
                self.build_error = (proc.stderr.strip().splitlines() or ["Index build failed."])[-1]
                return
            self.build_error = ""
            fresh = Index(self.index_path)
            with self.lock:
                old, self.index = self.index, fresh
            if old is not None:
                old.close()
        except (OSError, subprocess.SubprocessError) as e:
            self.build_error = f"Index build failed: {e}"
        finally:
            self.building = False

    def status(self) -> dict:
        self.check()
        idx = self.index
        meta = idx.meta if idx else {}
        key = paths.key_file()
        ledger = self.answerer.ledger
        loaded = float(meta["game_loaded_at"]) if meta.get("game_loaded_at") else None
        return {
            "pages": idx.page_count if idx else 0,
            "game_loaded_at": loaded,
            "game_loaded_label": time.strftime("%H:%M on %d %b", time.localtime(loaded)) if loaded else "",
            "mods": len(json.loads(meta.get("mods", "[]"))) if meta else 0,
            "building": self.building,
            "build_error": self.build_error,
            "key": key.exists() and key.stat().st_size > 0,
            "spent": round(ledger.month_total(), 4),
            "cap": ledger.cap,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "civspeak"
    state: State  # set by serve()

    def log_message(self, fmt, *args):  # keep the terminal quiet; never logs bodies
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in STATIC:
            name, ctype = STATIC[path]
            body = resources.files("civspeak").joinpath("web", name).read_bytes()
            self._send(HTTPStatus.OK, body, ctype)
        elif path == "/api/status":
            self._json(HTTPStatus.OK, self.state.status())
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/ask":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        # Only accept requests from our own page (blocks other websites posting to localhost).
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "expected JSON"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(min(length, 10_000)) or b"{}")
            question = str(payload.get("question", "")).strip()[:MAX_QUESTION]
        except (ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
            return
        if not question:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "empty question"})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self._answer(question)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _event(self, name: str, data) -> None:
        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def _answer(self, question: str) -> None:
        state = self.state
        state.check()
        idx = state.index
        if idx is None:
            message = (
                state.build_error or "The Civilopedia index is still being built from your game. Try again in a few seconds."
            )
            self._event("problem", {"kind": "no_index", "message": message})
            return
        hits = idx.search(question, k=6)
        self._event("entries", [{"score": h.score, **h.page} for h in hits])
        started = time.monotonic()
        try:
            first = None
            for kind, value in state.answerer.stream(question, hits):
                if kind == "delta":
                    if first is None:
                        first = time.monotonic() - started
                    self._event("delta", value)
                else:
                    r = value
                    self._event(
                        "done",
                        {
                            "text": r.text,
                            "used": r.used,
                            "cost": round(r.cost_usd, 5),
                            "spent": round(r.month_total, 4),
                            "cap": state.answerer.ledger.cap,
                            "first_text_seconds": round(first or 0, 2),
                            "seconds": round(time.monotonic() - started, 2),
                            "refused": r.refused,
                        },
                    )
                    log.info("answered in %.1fs (first text %.1fs), $%.4f", time.monotonic() - started, first or 0, r.cost_usd)
        except KeyMissing:
            self._event(
                "problem",
                {
                    "kind": "no_key",
                    "message": "No Claude API key is set up yet, so there is no spoken answer. "
                    "The matching Civilopedia entries are below.",
                },
            )
        except spend.CapReached as e:
            self._event(
                "problem",
                {
                    "kind": "cap",
                    "message": f"Monthly limit reached (${e.spent:.2f} of ${e.cap:.2f}). "
                    "Answers start again next month; the Civilopedia entries below still work.",
                },
            )
        except AnswerError as e:
            self._event("problem", {"kind": "api", "message": str(e)})


def serve(port: int = 8765, open_browser: bool = False, host: str = "127.0.0.1") -> int:
    answerer = Answerer()
    state = State(paths.index_file(), answerer)
    state.check()
    Handler.state = state
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://localhost:{port}"
    print(f"Civspeak is running at {url}  (Ctrl+C to stop)", flush=True)
    if state.building:
        print("Building the Civilopedia index from your game in the background...", flush=True)
    if open_browser:
        open_page(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


BRAVE = [
    "/mnt/c/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe",
    "/mnt/c/Program Files (x86)/BraveSoftware/Brave-Browser/Application/brave.exe",
]


def open_page(url: str) -> None:
    """Open the page as a Brave app window on Windows, or the default browser if Brave is missing."""
    for exe in BRAVE:
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    try:
        subprocess.Popen(["cmd.exe", "/c", "start", "", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        print(f"Open {url} in your browser.")
