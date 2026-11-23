"""Asking Civspeak's answer service: how players get answers with nothing to set up.

The service (answer_service.py, on Google Cloud Run) holds the key and writes the answer from the
Civilopedia entries this PC found, with the same instructions the player's own key would use. It
counts questions per install, a random anonymous ID made on first run and kept in settings.json, and
stops at a daily limit per install and a monthly limit for everyone. Nothing else is sent: no name,
no account, no audio (speech is turned into text on this PC).

Standard library only, so the Windows app carries nothing extra. CIVSPEAK_ANSWER_URL points it
somewhere else (a test server, or "off" for no service).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

from . import __version__, settings

log = logging.getLogger(__name__)

DEFAULT_URL = "https://civspeak-answers-cyjkea5pta-ew.a.run.app"
TIMEOUT_SECONDS = 60
WAKE_EVERY_SECONDS = 300
# Crockford's base32: no I, L, O or U, so an ID read out or typed into an email cannot be misread.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_id_lock = threading.Lock()


def make_install_id() -> str:
    """A new random install ID, such as "CS-7K4M-Q2XP-9FHT" (60 random bits)."""
    body = "".join(secrets.choice(ALPHABET) for _ in range(12))
    return f"CS-{body[:4]}-{body[4:8]}-{body[8:]}"


def is_install_id(text: str) -> bool:
    parts = text.split("-") if isinstance(text, str) else []
    return len(parts) == 4 and parts[0] == "CS" and all(len(p) == 4 and all(c in ALPHABET for c in p) for p in parts[1:])


def install_id() -> str:
    """This install's ID, made and kept in settings.json the first time it is needed."""
    with _id_lock:
        current = settings.load().install_id
        if is_install_id(current):
            return current
        made = make_install_id()
        settings.update(install_id=made)
        return made


def service_url() -> str:
    """The answer service's address ("" when CIVSPEAK_ANSWER_URL is "off")."""
    url = os.environ.get("CIVSPEAK_ANSWER_URL", DEFAULT_URL).strip()
    return "" if url.lower() == "off" else url.rstrip("/")


class LimitReached(RuntimeError):
    """The service said no more today (kind "daily", for this install) or this month (kind "monthly",
    for everyone)."""

    def __init__(self, kind: str, install: str):
        super().__init__(f"{kind} limit reached")
        self.kind = kind
        self.install_id = install


class AnswerService:
    def __init__(self, url: str | None = None, timeout: float = TIMEOUT_SECONDS):
        self._url = url
        self.timeout = timeout
        self._woken = -WAKE_EVERY_SECONDS

    @property
    def url(self) -> str:
        return self._url if self._url is not None else service_url()

    def warm_up(self) -> None:
        """Wake the service in the background, so a question after a quiet spell is answered as quickly as
        the rest: Cloud Run lets it sleep after about 15 minutes unused, and it takes some seconds to
        start. At most once every few minutes; a failure here is not a problem (the question says so)."""
        now = time.monotonic()
        if not self.url or now - self._woken < WAKE_EVERY_SECONDS:
            return
        self._woken = now
        threading.Thread(target=self._ping, name="wake-answers", daemon=True).start()

    def _ping(self) -> None:
        try:
            request = urllib.request.Request(self.url + "/healthz", headers={"User-Agent": f"Civspeak/{__version__}"})
            urllib.request.urlopen(request, timeout=self.timeout).close()
        except (urllib.error.URLError, OSError) as e:
            log.info("could not wake the answer service: %s", e)

    def stream(self, question: str, entries: list[str], spoken: bool = False) -> Iterator[tuple[str, object]]:
        """As answer.Answerer.stream: ("delta", text) while the answer streams, then ("done", Result).
        Raises LimitReached or answer.AnswerError, before any text when the service refuses."""
        from .answer import AnswerError, Result

        if not self.url:
            raise AnswerError("Answers are turned off on this PC (CIVSPEAK_ANSWER_URL).")
        me = install_id()
        body = json.dumps(
            {"install_id": me, "question": question, "entries": entries, "spoken": bool(spoken), "version": __version__}
        ).encode()
        request = urllib.request.Request(
            self.url + "/v1/answer",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": f"Civspeak/{__version__}"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise _refusal(e, me) from None
        except (urllib.error.URLError, OSError) as e:
            log.info("answer service unreachable: %s", e)
            raise AnswerError("Could not get an answer. Check the internet connection.") from None
        done = None
        with response:
            try:
                for raw in response:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(message.get("delta"), str):
                        yield "delta", message["delta"]
                    elif isinstance(message.get("done"), dict):
                        done = message["done"]
                    elif "error" in message:
                        raise AnswerError(str(message["error"]) or "Could not get an answer.")
            except OSError as e:  # TimeoutError included
                log.info("answer stream broke off: %s", e)
                raise AnswerError("The answer stopped part way. Ask again.") from None
        if done is None:
            raise AnswerError("The answer stopped part way. Ask again.")
        used = [u for u in done.get("used") or [] if isinstance(u, int) and 1 <= u <= len(entries)]
        yield (
            "done",
            Result(
                text=str(done.get("text") or ""),
                used=used,
                model=str(done.get("model") or ""),
                refused=bool(done.get("refused")),
            ),
        )


def _refusal(e: urllib.error.HTTPError, me: str) -> Exception:
    from .answer import AnswerError

    try:
        info = json.loads(e.read(10_000) or b"{}")
    except (ValueError, OSError):
        info = {}
    if not isinstance(info, dict):
        info = {}
    if e.code == 429 and info.get("limit") in ("daily", "monthly"):
        return LimitReached(info["limit"], me)
    log.info("answer service said %s: %s", e.code, info.get("error"))
    if e.code >= 500:
        return AnswerError("Answers are having trouble right now. Try again in a moment.")
    return AnswerError(f"Could not get an answer (error {e.code}).")
