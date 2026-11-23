"""Civspeak's answer service: writes players' answers with Civspeak's own key, within hard limits.

It runs on Google Cloud Run (service/), never on a player's PC. The app (online.py) sends the question
and the Civilopedia entries it found; the service answers with the same instructions, model and
streaming as answer.Answerer (it is an Answerer with the service's key), so it is no general-purpose
proxy: the instructions are fixed here, the model is the cheap one, the output is short and the input
is capped.

Before every answer, in one Firestore transaction, it counts the question against:
- the install's daily limit (an anonymous ID the app makes on first run; raised per ID on request),
- the internet address's daily limit (so making up new IDs does not get round the first), and
- a hard monthly cap in dollars for everyone: the answer's worst-case cost is reserved first and the
  real cost settled after, as spend.py does on a player's PC, so the cap can never be overshot.
Refusals are HTTP 429 with {"limit": "daily" | "monthly"}, which the app turns into its pop-up.

Settings are environment variables on the service, set with service/deploy.sh (no code change). The
limits' numbers live only there, never in this repository; without them the service answers nothing.
  CIVSPEAK_DAILY_QUESTIONS     questions a day per install
  CIVSPEAK_IP_DAILY_QUESTIONS  questions a day per internet address; raised IDs skip it
  CIVSPEAK_MONTHLY_CAP_USD     the hard monthly cap for everyone, in dollars
  CIVSPEAK_RAISED              raised daily limits per ID: "CS-AAAA-BBBB-CCCC=<n> CS-DDDD-EEEE-FFFF=<n>"
  CIVSPEAK_SERVICE_ANSWERS     quick | balanced | best (default quick)
  CIVSPEAK_SERVICE_KEY         the key (a Secret Manager secret; never logged)
Days and months are counted in UTC. Nothing a player asked is stored or logged: only counts, the
install ID, a hash of the address and the dollars spent.

Run it with `uvicorn civspeak.answer_service:app` (service/Dockerfile).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

from . import online
from .answer import CHOICES, Answerer, AnswerError, Result

log = logging.getLogger("civspeak.answer_service")

MAX_BODY_BYTES = 200_000
MAX_QUESTION_CHARS = 500
MAX_ENTRIES = 8
MAX_ENTRY_CHARS = 48_000  # all the entries together: about 16K tokens, under a fifth of a cent on Quick
KEEP_DAYS = 3  # daily counters carry an expiry this far ahead (a Firestore TTL policy deletes them)


class Refused(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # "daily" (this install or this address) or "monthly" (everyone)


class NotConfigured(RuntimeError):
    """A limit is missing from the service's settings: it answers nothing rather than guess one."""


@dataclass(frozen=True)
class Limits:
    daily: int
    ip_daily: int
    cap_usd: float
    raised: dict[str, int]

    def for_install(self, install: str) -> int:
        return self.raised.get(install, self.daily)


def _number(name: str, kind=int):
    text = os.environ.get(name, "").strip()
    try:
        value = kind(text)
    except ValueError:
        raise NotConfigured(f"{name} is not set to a number") from None
    if value < 0:
        raise NotConfigured(f"{name} is negative")
    return value


def parse_raised(text: str) -> dict[str, int]:
    """ "CS-AAAA-BBBB-CCCC=<n> CS-DDDD-EEEE-FFFF=<n>" (spaces or commas between) -> {id: limit}."""
    raised = {}
    for item in text.replace(",", " ").split():
        install, _, number = item.partition("=")
        install = install.strip().upper()
        if online.is_install_id(install) and number.strip().isdigit():
            raised[install] = int(number)
        else:
            log.error("ignoring CIVSPEAK_RAISED item %r", item)
    return raised


def limits() -> Limits:
    return Limits(
        daily=_number("CIVSPEAK_DAILY_QUESTIONS"),
        ip_daily=_number("CIVSPEAK_IP_DAILY_QUESTIONS"),
        cap_usd=_number("CIVSPEAK_MONTHLY_CAP_USD", float),
        raised=parse_raised(os.environ.get("CIVSPEAK_RAISED", "")),
    )


def periods(now: float | None = None) -> tuple[str, str]:
    t = time.gmtime(now if now is not None else time.time())
    return time.strftime("%Y-%m-%d", t), time.strftime("%Y-%m", t)


def address_hash(ip: str, day: str) -> str:
    """The address as counted: hashed with the day, so no address is stored as it is."""
    return hashlib.sha256(f"{day}|{ip}".encode()).hexdigest()[:24]


# -- Counting ----------------------------------------------------------------------------------


class MemoryStore:
    """The counters in memory: for tests, and `civspeak serve-answers --memory` on a PC."""

    def __init__(self):
        self.lock = threading.Lock()
        self.installs: dict[tuple[str, str], int] = {}
        self.addresses: dict[tuple[str, str], int] = {}
        self.months: dict[str, float] = {}

    def reserve(self, *, install, address, day, month, allowed, ip_allowed, cap_usd, usd) -> None:
        with self.lock:
            asked = self.installs.get((day, install), 0)
            if asked >= allowed:
                raise Refused("daily")
            from_address = self.addresses.get((day, address), 0)
            if ip_allowed is not None and from_address >= ip_allowed:
                raise Refused("daily")
            if self.months.get(month, 0.0) + usd > cap_usd:
                raise Refused("monthly")
            self.installs[(day, install)] = asked + 1
            self.addresses[(day, address)] = from_address + 1
            self.months[month] = self.months.get(month, 0.0) + usd

    def settle(self, month: str, delta_usd: float) -> None:
        with self.lock:
            self.months[month] = self.months.get(month, 0.0) + delta_usd

    def spent(self, month: str) -> float:
        return self.months.get(month, 0.0)


class FirestoreStore:
    """The counters in Firestore (Native mode), so every Cloud Run instance shares them exactly.

    civspeak_installs/{day}_{install} {count}, civspeak_addresses/{day}_{hash} {count} (both with an
    `expires` time for the TTL policy) and civspeak_months/{month} {usd}."""

    def __init__(self, client=None):
        from google.cloud import firestore

        self.firestore = firestore
        self.db = client or firestore.Client()

    def reserve(self, *, install, address, day, month, allowed, ip_allowed, cap_usd, usd) -> None:
        firestore = self.firestore
        install_ref = self.db.collection("civspeak_installs").document(f"{day}_{install}")
        address_ref = self.db.collection("civspeak_addresses").document(f"{day}_{address}")
        month_ref = self.db.collection("civspeak_months").document(month)
        expires = _expiry()

        @firestore.transactional
        def count(transaction):
            snaps = {s.reference.path: s for s in transaction.get_all([install_ref, address_ref, month_ref])}

            def field(ref, name, default):
                snap = snaps.get(ref.path)
                value = snap.get(name) if snap is not None and snap.exists else None
                return value if isinstance(value, (int, float)) else default

            asked = int(field(install_ref, "count", 0))
            from_address = int(field(address_ref, "count", 0))
            spent = float(field(month_ref, "usd", 0.0))
            if asked >= allowed:
                raise Refused("daily")
            if ip_allowed is not None and from_address >= ip_allowed:
                raise Refused("daily")
            if spent + usd > cap_usd:
                raise Refused("monthly")
            transaction.set(install_ref, {"count": asked + 1, "expires": expires})
            transaction.set(address_ref, {"count": from_address + 1, "expires": expires})
            transaction.set(month_ref, {"usd": spent + usd})

        count(self.db.transaction())

    def settle(self, month: str, delta_usd: float) -> None:
        ref = self.db.collection("civspeak_months").document(month)
        ref.set({"usd": self.firestore.Increment(delta_usd)}, merge=True)

    def spent(self, month: str) -> float:
        snap = self.db.collection("civspeak_months").document(month).get()
        value = snap.get("usd") if snap.exists else 0.0
        return float(value or 0.0)


def _expiry():
    import datetime

    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=KEEP_DAYS)


class QuotaLedger:
    """What Answerer.stream_entries reserves and settles with: one question's limits and spend."""

    def __init__(self, store, install: str, address: str, limits: Limits, now: float | None = None):
        self.store, self.install, self.address, self.limits = store, install, address, limits
        self.day, self.month = periods(now)

    def reserve(self, usd: float):
        allowed = self.limits.for_install(self.install)
        raised = self.install in self.limits.raised
        self.store.reserve(
            install=self.install,
            address=address_hash(self.address, self.day),
            day=self.day,
            month=self.month,
            allowed=allowed,
            ip_allowed=None if raised else self.limits.ip_daily,
            cap_usd=self.limits.cap_usd,
            usd=usd,
        )
        return _Reservation(self, usd)


class _Reservation:
    def __init__(self, ledger: QuotaLedger, usd: float):
        self.ledger, self.usd = ledger, usd

    def settle(self, usd: float, **_details) -> float:
        # A part-way failure is settled at the full reservation (answer.py), so the cap still holds.
        if usd != self.usd:
            self.ledger.store.settle(self.ledger.month, usd - self.usd)
        self.ledger.cost = usd
        return 0.0


# -- The service -------------------------------------------------------------------------------


class ServiceKey:
    """The service's own key, from its environment (a Secret Manager secret on Cloud Run)."""

    where = "CIVSPEAK_SERVICE_KEY"

    def read(self) -> str:
        from .keys import KeyMissing

        key = os.environ.get("CIVSPEAK_SERVICE_KEY", "").strip()
        if not key:
            raise KeyMissing("CIVSPEAK_SERVICE_KEY is not set.")
        return key

    def present(self) -> bool:
        return bool(os.environ.get("CIVSPEAK_SERVICE_KEY", "").strip())


class BadRequest(ValueError):
    pass


def parse_request(raw: bytes) -> tuple[str, str, list[str], bool]:
    """(install, question, entries, spoken) from the app's request, or BadRequest saying what is wrong."""
    try:
        body = json.loads(raw)
    except ValueError:
        raise BadRequest("expected JSON") from None
    if not isinstance(body, dict):
        raise BadRequest("expected a JSON object")
    install = body.get("install_id")
    question = body.get("question")
    entries = body.get("entries")
    if not isinstance(install, str) or not online.is_install_id(install):
        raise BadRequest("install_id is missing or not a Civspeak ID")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise BadRequest(f"question must be 1 to {MAX_QUESTION_CHARS} characters")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES or not all(isinstance(e, str) for e in entries):
        raise BadRequest(f"entries must be a list of at most {MAX_ENTRIES} strings")
    if sum(len(e) for e in entries) > MAX_ENTRY_CHARS:
        raise BadRequest(f"entries must be at most {MAX_ENTRY_CHARS} characters in all")
    return install, question.strip(), entries, body.get("spoken") is True


class Service:
    def __init__(self, store=None, answerer: Answerer | None = None):
        self._store = store
        choice = os.environ.get("CIVSPEAK_SERVICE_ANSWERS", "quick")
        self.answerer = answerer or Answerer(
            ledger=object(), key_store=ServiceKey(), choice=choice if choice in CHOICES else "quick", service=False
        )

    @property
    def store(self):
        if self._store is None:
            self._store = FirestoreStore()
        return self._store

    def start(self, install: str, address: str, question: str, entries: list[str], spoken: bool):
        """The answer's lines, after the limits were checked and the first text (or the end) arrived.
        Raises Refused, or AnswerError when nothing could be answered."""
        ledger = QuotaLedger(self.store, install, address, limits())
        stream = self.answerer.stream_entries(question, entries, spoken, ledger=ledger)
        first = next(stream)  # reserves (or refuses) and waits for the first words
        return self._lines(install, first, stream, ledger)

    def _lines(self, install: str, first, stream, ledger: QuotaLedger) -> Iterator[bytes]:
        started = time.monotonic()
        try:
            for kind, value in _chain(first, stream):
                if kind == "delta":
                    yield _line({"delta": value})
                elif isinstance(value, Result):
                    done = {"text": value.text, "used": value.used, "model": value.model, "refused": value.refused}
                    yield _line({"done": done})
            log.info("answered %s in %.1fs, $%.5f", install, time.monotonic() - started, getattr(ledger, "cost", 0.0))
        except AnswerError as e:
            log.warning("answer for %s stopped part way: %s", install, e)
            yield _line({"error": str(e)})


def _chain(first, rest):
    yield first
    yield from rest


def _line(message: dict) -> bytes:
    return (json.dumps(message) + "\n").encode()


def client_address(headers, fallback: str) -> str:
    """The player's address: Cloud Run's front end puts it first in X-Forwarded-For."""
    forwarded = headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or fallback


def make_app(service: Service | None = None):
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
    from starlette.routing import Route

    svc = service or Service()

    async def answer(request):
        length = request.headers.get("content-length")
        if length is None or not length.isdigit() or int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": f"send at most {MAX_BODY_BYTES} bytes, with a Content-Length"}, 413)
        try:
            install, question, entries, spoken = parse_request(await request.body())
        except BadRequest as e:
            return JSONResponse({"error": str(e)}, 400)
        address = client_address(request.headers, request.client.host if request.client else "")
        try:
            lines = await run_in_threadpool(svc.start, install, address, question, entries, spoken)
        except Refused as e:
            log.info("refused %s: %s limit", install, e.kind)
            return JSONResponse({"limit": e.kind}, 429)
        except NotConfigured as e:
            log.error("not answering: %s", e)
            return JSONResponse({"error": "Answers are not available right now."}, 503)
        except AnswerError as e:
            log.warning("no answer for %s: %s", install, e)
            return JSONResponse({"error": str(e)}, 502)
        return StreamingResponse(lines, media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})

    async def health(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/v1/answer", answer, methods=["POST"]), Route("/healthz", health)])


def __getattr__(name: str):
    # `uvicorn civspeak.answer_service:app` builds the app on first use, so importing this module
    # (tests, the Windows app's analysis) never needs Starlette or Firestore.
    if name == "app":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        service = Service()
        # Ready before the first question, not during it: a woken instance starts here (online.py pings
        # /healthz when a player presses the talk key), and Cloud Run waits for this before sending it any.
        _ = service.store
        service.answerer.warm_up()
        globals()["app"] = make_app(service)
        return globals()["app"]
    raise AttributeError(name)
