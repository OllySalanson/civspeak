"""A small local web server for the answer page.

GET  /                   the page
GET  /api/status         index age, entry count, key present, this month's spend, voice and mic
POST /api/ask            {"question": "...", "speak": true, "spoken": false} -> text/event-stream of:
                           entries  the matched Civilopedia entries (always sent first)
                           delta    answer text as it streams
                           say      {"url": ...} the next sentence, spoken (when speak is on)
                           done     final answer, entries used, cost and month total
                           problem  why there is no answer (no key, monthly limit, API error)
GET  /api/speech/ID/N    sentence N of an answer as WAV (waits until George has said it)
POST /api/listen         a recorded question (audio/wav) -> {"text": "...", "seconds": ...}
GET  /api/talk           text/event-stream for the page: "talk" events ({"key": "down"} or "up") from the
                           WSL helper, and in the Windows app everything its own microphone and speakers
                           do (see conversation.py): mic, level, question, the answer's events, speaking
POST /api/hotkey         {"key": "down"|"up"} from the Windows helper -> {"listening": true} when a page took it
POST /api/wake           {} -> starts loading the voice and speech recognizer if they were unloaded while idle
POST /api/mic            {"key": "down"|"up"|"cancel"} the page's microphone button, in the Windows app
POST /api/hush           {} -> George stops talking, in the Windows app
POST /api/voice          {"on": true|false} the page's Voice on/off, for questions asked in the Windows app
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import secrets
import select
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import index as index_mod
from . import paths, spend, system
from .answer import Answerer, AnswerError, KeyMissing
from .hearing import BadAudio, Ears, Heard
from .idle import IdleUnloader
from .latency import LatencyLog
from .search import Index
from .speech import Narrator, SentenceSplitter, Voice

log = logging.getLogger(__name__)

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/recorder.js": ("recorder.js", "text/javascript; charset=utf-8"),
}
MAX_QUESTION = 500
MAX_RECORDING = 4_000_000  # bytes: about two minutes of 16 kHz WAV; the page stops at 15 seconds
STALE_CHECK_SECONDS = 10
TALK_PING_SECONDS = 20  # a comment line this often lets a closed page's connection be noticed
TALK_KEYS = ("down", "up")
MIC_KEYS = ("down", "up", "cancel")
LISTEN_PAIRS_SECONDS = 30  # a spoken question is asked this soon after it was heard
SPEECH_URL = re.compile(r"^/api/speech/([A-Za-z0-9_-]{6,32})/(\d{1,3})$")

# What George says when there is no answer to read out.
SPOKEN_PROBLEMS = {
    "no_index": "The Civilopedia is still being built from your game. Try again in a moment.",
    "no_key": "There's no Claude key set up yet, so I can't answer. The entries are on screen.",
    "cap": "The monthly limit is reached, so I can't answer until next month. The entries are on screen.",
    "api": "Claude isn't answering right now. The entries are on screen.",
}
NOT_HEARD = "Sorry, I didn't catch that."


class State:
    """The current index, plus a background rebuild when the game's cache or log changes."""

    def __init__(
        self,
        index_dir: Path,
        answerer: Answerer,
        voice: Voice | None = None,
        ears: Ears | None = None,
        latency: LatencyLog | None = None,
    ):
        self.index_dir = index_dir
        self.answerer = answerer
        self.voice = voice or Voice()
        self.narrator = Narrator(self.voice)
        self.ears = ears or Ears()
        self.latency = latency or LatencyLog(paths.latency_file())
        self.unloader = IdleUnloader([self.voice, self.ears], paths.idle_unload_seconds())
        self.last_listen: dict = {}
        self._waking = threading.Lock()
        self.lock = threading.Lock()
        self.index: Index | None = None
        self.building = False
        self.build_error = ""
        self.game_language = ""  # the game's text language from AppOptions.txt (Civspeak builds English)
        self._last_check = 0.0
        self._checking = threading.Lock()
        current = index_mod.current(index_dir)
        if current is not None:
            self._use(current)

    def check(self) -> None:
        # One request at a time looks: two would open (and later close) the same new index twice.
        if not self._checking.acquire(blocking=False):
            return
        try:
            self._check()
        finally:
            self._checking.release()

    def _check(self) -> None:
        now = time.monotonic()
        if self.building or now - self._last_check < STALE_CHECK_SECONDS and self.index is not None:
            return
        self._last_check = now
        current = index_mod.current(self.index_dir)
        if current is not None and (self.index is None or self.index.path != current):
            self._use(current)  # built by `civspeak build` while this server was running
        try:
            game = paths.discover()
            self.game_language = game.display_language
            stale = index_mod.is_stale(current, game)
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
                system.civspeak_command("build"),
                capture_output=True,
                text=True,
                timeout=600,
                **system.background_process_options(),
            )
            if proc.returncode != 0:
                self.build_error = (proc.stderr.strip().splitlines() or ["Index build failed."])[-1]
                return
            self.build_error = ""
            current = index_mod.current(self.index_dir)
            if current is not None:
                self._use(current)
        except (OSError, subprocess.SubprocessError) as e:
            self.build_error = f"Index build failed: {e}"
        finally:
            self.building = False

    def _use(self, path: Path) -> None:
        """Switch to the index at path, then remove the old one (Windows lets it go only once it is closed)."""
        try:
            fresh = Index(path)
        except sqlite3.Error as e:
            self.build_error = f"The Civilopedia index could not be opened: {e}"
            return
        with self.lock:
            old, self.index = self.index, fresh
        if old is not None:
            old.close()
        index_mod.remove_old(self.index_dir)

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
            "game_language": self.game_language,
            "key": key.exists() and key.stat().st_size > 0,
            "spent": round(ledger.month_total(), 4),
            "cap": ledger.cap,
            "voice": {"problem": self.voice.problem(), "ready": self.voice.loaded},
            "mic": {"problem": self.ears.problem(), "ready": self.ears.loaded},
        }

    def warm_up(self) -> None:
        """Get everything a question needs ready in the background, so the first question is not slow:
        George and Whisper loaded and run once, and Claude's client library imported (about a second)."""
        self.wake()
        threading.Thread(target=self.answerer.warm_up, name="warm-claude", daemon=True).start()
        self.unloader.start()

    def wake(self) -> None:
        """Load George and Whisper again if they were unloaded while idle. Called as soon as the talk
        key or the microphone is pressed, so they load while the player is still speaking."""
        if not self._waking.acquire(blocking=False):
            return  # already waking
        try:
            if not self.voice.loaded and not self.voice.problem():
                self.narrator.warm_up()
            if not self.ears.loaded and not self.ears.problem():
                threading.Thread(target=self._warm_ears, name="warm-ears", daemon=True).start()
        finally:
            self._waking.release()

    def hear(self, audio: bytes) -> Heard:
        """What was said in a recorded question (a WAV), noting how long it took for the latency log."""
        started, cold = time.monotonic(), not self.ears.loaded
        heard = self.ears.transcribe(audio, self.index)
        self.last_listen = {
            "at": time.monotonic(),
            "listen_s": round(time.monotonic() - started, 2),
            "audio_s": heard.audio_seconds,
            "ears_cold": cold,
        }
        return heard

    def _warm_ears(self) -> None:
        try:
            self.ears.warm_up(self.index)
        except RuntimeError as e:
            log.warning("%s", e)


class Talk:
    """The open pages' event streams. Hold-to-talk presses from the WSL helper go to the page that
    opened most recently only, so a second open page never records (and pays for) the same question;
    what the Windows app's own microphone and speakers do goes to every page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pages: list[tuple[queue.SimpleQueue, socket.socket]] = []

    def join(self, conn: socket.socket) -> queue.SimpleQueue:
        q: queue.SimpleQueue = queue.SimpleQueue()
        with self.lock:
            self.pages.append((q, conn))
        return q

    def leave(self, q: queue.SimpleQueue) -> None:
        with self.lock:
            self.pages = [p for p in self.pages if p[0] is not q]

    def press(self, key: str) -> bool:
        """Hand a key change to the newest page that is still connected; False if there is none."""
        with self.lock:
            while self.pages:
                q, conn = self.pages[-1]
                if not _closed(conn):
                    q.put(("talk", {"key": key}))
                    return True
                self.pages.pop()
        return False

    def broadcast(self, event: str, data) -> None:
        """Tell every open page (a page that has gone is dropped when its stream next writes)."""
        with self.lock:
            for q, _ in self.pages:
                q.put((event, data))


def _closed(conn: socket.socket) -> bool:
    """Whether the page at the other end has gone (an event stream's page never sends anything)."""
    try:
        readable, _, _ = select.select([conn], [], [], 0)
        return bool(readable) and conn.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "civspeak"
    state: State  # set by make_server()
    talk: Talk  # set by make_server()
    conversation = None  # the Windows app's own microphone and speakers (conversation.Conversation), set by make_server()

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
            self._json(HTTPStatus.OK, self._status())
        elif path == "/api/talk":
            self._talk_stream()
        elif m := SPEECH_URL.match(path):
            self._speech(m.group(1), int(m.group(2)))
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _status(self) -> dict:
        status = self.state.status()
        status["native"] = self.conversation is not None
        if self.conversation is not None:
            status["mic"]["problem"] = self.conversation.problem()
        return status

    def _speech(self, answer_id: str, n: int) -> None:
        clip = self.state.narrator.clip(answer_id, n)
        if clip is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "no such sentence"})
            return
        try:
            wav = clip.result(timeout=60)
        except CancelledError:
            self._json(HTTPStatus.GONE, {"error": "a newer answer replaced this one"})
            return
        except Exception as e:  # synthesis failed; the page just skips this sentence
            log.warning("could not voice a sentence: %s", e)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "could not voice this sentence"})
            return
        self._send(HTTPStatus.OK, wav, "audio/wav")

    def _talk_stream(self) -> None:
        """The page listens here for the ` key held in a game (see the Windows helper), and in the Windows
        app for its microphone, its answers to spoken questions and George speaking."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = self.talk.join(self.connection)
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    event, data = q.get(timeout=TALK_PING_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                else:
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
        except OSError:
            pass  # the page closed or reloaded
        finally:
            self.talk.leave(q)
            self.close_connection = True

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/listen":
            self._listen(speak="speak=1" in self.path)
            return
        if path not in ("/api/ask", "/api/hotkey", "/api/wake", "/api/mic", "/api/hush", "/api/voice"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        # Only accept requests from our own page and helper (blocks other websites posting to localhost).
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "expected JSON"})
            return
        if path == "/api/hotkey":
            self._hotkey()
            return
        if path == "/api/wake":
            self.state.wake()
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path in ("/api/mic", "/api/hush", "/api/voice"):
            self._native(path)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(min(length, 10_000)) or b"{}")
            question = str(payload.get("question", "")).strip()[:MAX_QUESTION]
            speak = bool(payload.get("speak", False))
            spoken = bool(payload.get("spoken", False))
        except (ValueError, json.JSONDecodeError, AttributeError):
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
        speaker = self._speaker(speak)
        try:
            Answering(self.state, self._event).run(question, speaker, spoken)
        except (BrokenPipeError, ConnectionResetError):
            if speaker:
                speaker.cancel()

    def _hotkey(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            key = json.loads(self.rfile.read(min(length, 1000)) or b"{}").get("key")
        except (ValueError, json.JSONDecodeError, AttributeError):
            key = None
        if key not in TALK_KEYS:
            self._json(HTTPStatus.BAD_REQUEST, {"error": 'expected {"key": "down"} or {"key": "up"}'})
            return
        if key == "down":
            self.state.wake()
        self._json(HTTPStatus.OK, {"listening": self.talk.press(key)})

    def _native(self, path: str) -> None:
        """The page's microphone button, Esc and Voice on/off, for the Windows app's own microphone and speakers."""
        conversation = self.conversation
        if conversation is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "this Civspeak records and plays in the page"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(min(length, 1000)) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
            return
        if path == "/api/hush":
            conversation.hush()
        elif path == "/api/voice":
            conversation.speak = bool(payload.get("on", True))
        else:
            key = payload.get("key")
            if key not in MIC_KEYS:
                self._json(HTTPStatus.BAD_REQUEST, {"error": 'expected {"key": "down"}, "up" or "cancel"'})
                return
            conversation.press(key)
        self._json(HTTPStatus.OK, {"ok": True})

    def _speaker(self, speak: bool) -> Speaker | None:
        """Someone to voice this answer, if the page wants it spoken and George is available. In the
        Windows app he speaks here, and an answer to a spoken question still going goes quiet."""
        if self.conversation is not None:
            return self.conversation.typed(self._event, speak)
        if not speak or self.state.voice.problem():
            return None
        return Speaker(self.state.narrator, self._event)

    def _listen(self, speak: bool) -> None:
        # audio/wav cannot be posted cross-site without a CORS preflight, which this server never grants.
        if self.headers.get("Content-Type", "").split(";")[0] != "audio/wav":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "expected audio/wav"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_RECORDING:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "recording missing or too long"})
            return
        audio = self.rfile.read(length)
        if self.state.ears.problem():
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": self.state.ears.problem()})
            return
        try:
            heard = self.state.hear(audio)
        except BadAudio as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            return
        except RuntimeError as e:  # the model could not be loaded
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(e)})
            return
        reply = {"text": heard.text, "seconds": heard.seconds, "audio_seconds": heard.audio_seconds}
        if not heard.text and speak and not self.state.voice.problem():
            narrator = self.state.narrator
            answer_id = secrets.token_urlsafe(9)
            narrator.start(answer_id)
            reply["say"] = {"url": f"/api/speech/{answer_id}/{narrator.say(answer_id, NOT_HEARD)}", "text": NOT_HEARD}
        self._json(HTTPStatus.OK, reply)

    def _event(self, name: str, data) -> None:
        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()


class Answering:
    """Answers one question: the matched entries, Claude's answer as it streams and George reading it
    out, each told to event(name, data): the asking page's answer stream, or every open page for a
    question asked out loud in the Windows app."""

    def __init__(self, state: State, event):
        self.state = state
        self._event = event

    def _problem(self, kind: str, message: str, speaker: Speaker | None) -> None:
        self._event("problem", {"kind": kind, "message": message})
        if speaker:
            speaker.say(SPOKEN_PROBLEMS[kind])

    def run(self, question: str, speaker: Speaker | None, spoken: bool) -> None:
        timing = {"spoken": spoken, "voice_cold": bool(speaker) and not self.state.voice.loaded}
        listened = self.state.last_listen
        if spoken and listened and time.monotonic() - listened["at"] < LISTEN_PAIRS_SECONDS:
            timing |= {k: v for k, v in listened.items() if k != "at"}
        received = time.monotonic()
        try:
            timing["outcome"] = self._answer_timed(question, speaker, spoken, timing)
        finally:
            timing["total_s"] = round(time.monotonic() - received, 2)  # the whole answer written
            if speaker:
                timing["first_voice_s"] = speaker.first_voice_seconds(since=received)
            self.state.latency.record({k: v for k, v in timing.items() if v is not None})

    def _answer_timed(self, question: str, speaker: Speaker | None, spoken: bool, timing: dict) -> str:
        state = self.state
        state.check()
        idx = state.index
        if idx is None:
            message = (
                state.build_error or "The Civilopedia index is still being built from your game. Try again in a few seconds."
            )
            self._problem("no_index", message, speaker)
            return "no_index"
        searched = time.monotonic()
        hits = idx.search(question, k=6)
        timing["search_ms"] = round((time.monotonic() - searched) * 1000)
        self._event("entries", [{"score": h.score, **h.page} for h in hits])
        started = time.monotonic()
        try:
            first = None
            for kind, value in state.answerer.stream(question, hits, spoken=spoken):
                if kind == "delta":
                    if first is None:
                        first = time.monotonic() - started
                    self._event("delta", value)
                    if speaker:
                        speaker.feed(value)
                else:
                    r = value
                    if speaker:
                        speaker.finish()
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
                    timing["first_text_s"] = round(first or 0, 2)
                    timing["cost"] = round(r.cost_usd, 5)
            return "answered"
        except KeyMissing:
            self._problem(
                "no_key",
                "No Claude API key is set up yet, so there is no spoken answer. The matching Civilopedia entries are below.",
                speaker,
            )
            return "no_key"
        except spend.CapReached as e:
            self._problem(
                "cap",
                f"Monthly limit reached (${e.spent:.2f} of ${e.cap:.2f}). "
                "Answers start again next month; the Civilopedia entries below still work.",
                speaker,
            )
            return "cap"
        except AnswerError as e:
            self._problem("api", str(e), speaker)
            return "api"


class Speaker:
    """Voices one answer: cuts the streamed text into sentences and tells the page where each is. In the
    Windows app `speakers` (audio.Speakers) also plays each sentence here, as soon as it is voiced."""

    def __init__(self, narrator: Narrator, event, speakers=None):
        self.narrator = narrator
        self.event = event
        self.answer_id = secrets.token_urlsafe(9)
        self.splitter = SentenceSplitter()
        self.first_ready: float | None = None  # when George's first sentence was ready to play
        self.cancelled = False
        narrator.start(self.answer_id)
        self.speakers = speakers
        self.turn = speakers.begin() if speakers else 0

    def say(self, sentence: str) -> None:
        if self.cancelled:
            return
        n = self.narrator.say(self.answer_id, sentence)
        clip = self.narrator.clip(self.answer_id, n)
        if n == 0 and clip is not None:
            clip.add_done_callback(lambda f: setattr(self, "first_ready", time.monotonic()))
        if self.speakers and clip is not None:
            self.speakers.play(clip, self.turn)
        self.event("say", {"url": f"/api/speech/{self.answer_id}/{n}", "text": sentence})

    def first_voice_seconds(self, since: float, wait: float = 30) -> float | None:
        """Seconds from `since` until the first sentence was voiced (waiting for it if need be)."""
        clip = self.narrator.clip(self.answer_id, 0)
        if clip is None:
            return None
        try:
            clip.result(timeout=wait)
        except Exception:  # cancelled by a newer question, or synthesis failed
            return None
        return round((self.first_ready or time.monotonic()) - since, 2)

    def feed(self, text: str) -> None:
        for sentence in self.splitter.feed(text):
            self.say(sentence)

    def finish(self) -> None:
        for sentence in self.splitter.flush():
            self.say(sentence)

    def cancel(self) -> None:
        """Say nothing more of this answer."""
        self.cancelled = True
        self.narrator.cancel(self.answer_id)


class Server(ThreadingHTTPServer):
    """The answer page's server; .state, .talk and .conversation are what its requests share."""

    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second server take a port that is already in use.
    allow_reuse_address = not system.IS_WINDOWS
    state: State
    talk: Talk
    conversation = None

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: and no other program can take ours
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port: int = 8765, host: str = "127.0.0.1", any_port: bool = False, native: bool = False) -> Server:
    """The server, listening (not yet serving) with the index checked and the voice warming up.

    any_port: if `port` is taken, listen on a free one instead (the Windows app opens its own page,
    so it does not mind which); otherwise the OSError is raised.
    native: record and play George on this PC's own microphone and speakers (the Windows app), not
    in the page.
    """
    state = State(paths.data_dir(), Answerer())
    talk = Talk()
    conversation = None
    if native:
        from .conversation import Conversation

        conversation = Conversation(state, talk)
    handler = type("PageHandler", (Handler,), {"state": state, "talk": talk, "conversation": conversation})
    try:
        httpd = Server((host, port), handler)
    except OSError:
        if not any_port:
            raise
        httpd = Server((host, 0), handler)
    httpd.state, httpd.talk, httpd.conversation = state, talk, conversation
    state.check()
    state.warm_up()
    return httpd


def serve(port: int = 8765, open_browser: bool = False, host: str = "127.0.0.1", watch_stdin: bool = False) -> int:
    httpd = make_server(port, host)
    url = f"http://localhost:{port}"
    print(f"Civspeak is running at {url}  (Ctrl+C to stop)", flush=True)
    if httpd.state.building:
        print("Building the Civilopedia index from your game in the background...", flush=True)
    if open_browser:
        open_page(url)
    if watch_stdin:
        threading.Thread(target=_stop_when_stdin_closes, args=(httpd,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def _stop_when_stdin_closes(httpd: ThreadingHTTPServer) -> None:
    # The Windows helper holds our standard input open; when it turns Civspeak off (or goes away), stop.
    while sys.stdin.buffer.read(4096):
        pass
    print("Stopping: the Windows helper closed.", flush=True)
    httpd.shutdown()


BRAVE = [
    "/mnt/c/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe",
    "/mnt/c/Program Files (x86)/BraveSoftware/Brave-Browser/Application/brave.exe",
]


def _brave_paths() -> list[str]:
    if not system.IS_WINDOWS:
        return BRAVE
    folders = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    return [os.path.join(f, "BraveSoftware", "Brave-Browser", "Application", "brave.exe") for f in folders if f]


def open_page(url: str) -> None:
    """Open the page as a Brave app window on Windows, or the default browser if Brave is missing."""
    for exe in _brave_paths():
        if os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    try:
        if system.IS_WINDOWS:
            os.startfile(url)  # the player's default browser
        else:
            subprocess.Popen(["cmd.exe", "/c", "start", "", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        print(f"Open {url} in your browser.")
