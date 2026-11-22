"""The answer service (answer_service.py) and the app's side of it (online.py), with a fake Claude and
counters in memory. Nothing here reaches the internet."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from civspeak import answer, answer_service, online, server, settings, spend
from civspeak.answer import Answerer, AnswerError
from civspeak.answer_service import Limits, MemoryStore, QuotaLedger, Refused, Service, make_app, parse_raised
from civspeak.search import Hit

ME = "CS-7K4M-Q2XP-9FHT"
OTHER = "CS-0000-1111-2222"


class FakeClaude:
    """Stands in for the Anthropic client: streams the chunks and records what it was asked."""

    def __init__(self, chunks=("Build it ", "on flat land.", "\nUSED: 1"), fail=None):
        self.calls = []
        self.chunks, self.fail = list(chunks), fail
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        text = "".join(self.chunks)
        usage = SimpleNamespace(
            input_tokens=2700, output_tokens=80, cache_read_input_tokens=0, cache_creation_input_tokens=0, iterations=None
        )
        final = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], usage=usage, model=kwargs["model"], stop_reason="end_turn"
        )
        chunks = self.chunks

        class Stream:
            text_stream = iter(chunks)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return final

        return Stream()


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("CIVSPEAK_SERVICE_KEY", "service-key-not-real")
    for name in ("CIVSPEAK_DAILY_QUESTIONS", "CIVSPEAK_IP_DAILY_QUESTIONS", "CIVSPEAK_MONTHLY_CAP_USD", "CIVSPEAK_RAISED"):
        monkeypatch.delenv(name, raising=False)
    store = MemoryStore()
    svc = Service(store=store)
    claude = FakeClaude()
    svc.answerer._client_for = lambda key: claude
    return SimpleNamespace(svc=svc, store=store, claude=claude, client=TestClient(make_app(svc)))


def post(client, body, **headers):
    return client.post("/v1/answer", content=json.dumps(body), headers={"Content-Type": "application/json", **headers})


def request(install=ME, question="Where can I build a Campus?", entries=("Campus\nPlacement: flat land",), spoken=False):
    return {"install_id": install, "question": question, "entries": list(entries), "spoken": spoken}


def lines(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


# -- The service ---------------------------------------------------------------------------------


def test_answers_with_the_fixed_instructions_and_the_quick_model(service):
    r = post(service.client, request(spoken=True))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    out = lines(r)
    assert "".join(m["delta"] for m in out if "delta" in m) == "Build it on flat land."
    assert out[-1] == {"done": {"text": "Build it on flat land.", "used": [1], "model": "claude-haiku-5-5", "refused": False}}
    call = service.claude.calls[0]
    assert call["model"] == "claude-haiku-5-5" and "fallbacks" not in call
    assert call["system"][0]["text"] == answer.SYSTEM_PROMPT  # the app cannot change the instructions
    expected = answer.build_message("Where can I build a Campus?", ["Campus\nPlacement: flat land"], True)
    assert call["messages"][0]["content"] == expected
    # The real cost was settled: about 0.03 cents, far below the reservation.
    month = answer_service.periods()[1]
    assert 0 < service.store.spent(month) < 0.001


def test_daily_limit_per_install(service, monkeypatch):
    monkeypatch.setenv("CIVSPEAK_DAILY_QUESTIONS", "2")
    assert post(service.client, request()).status_code == 200
    assert post(service.client, request()).status_code == 200
    r = post(service.client, request())
    assert r.status_code == 429 and r.json() == {"limit": "daily", "allowed": 2}
    assert len(service.claude.calls) == 2  # the refused question never reached Claude
    assert post(service.client, request(install=OTHER)).status_code == 200  # another computer is unaffected


def test_raised_limit_for_one_install_skips_the_address_limit(service, monkeypatch):
    monkeypatch.setenv("CIVSPEAK_DAILY_QUESTIONS", "1")
    monkeypatch.setenv("CIVSPEAK_IP_DAILY_QUESTIONS", "1")
    monkeypatch.setenv("CIVSPEAK_RAISED", f"{ME.lower()}=3, not-an-id=9")
    for _ in range(3):
        assert post(service.client, request()).status_code == 200
    assert post(service.client, request()).json() == {"limit": "daily", "allowed": 3}


def test_address_limit_stops_made_up_ids(service, monkeypatch):
    monkeypatch.setenv("CIVSPEAK_IP_DAILY_QUESTIONS", "2")
    ids = [online.make_install_id() for _ in range(3)]
    codes = [post(service.client, request(install=i), **{"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}).status_code for i in ids]
    assert codes == [200, 200, 429]
    assert post(service.client, request(install=ids[2]), **{"X-Forwarded-For": "198.51.100.2"}).status_code == 200


def test_monthly_cap_reserves_the_worst_case_first(service, monkeypatch):
    monkeypatch.setenv("CIVSPEAK_MONTHLY_CAP_USD", "0.0005")  # below one answer's worst case
    r = post(service.client, request())
    assert r.status_code == 429 and r.json()["limit"] == "monthly"
    assert service.claude.calls == []


def test_a_failed_call_is_refunded_and_says_so(service):
    import anthropic
    import httpx

    service.claude.fail = anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    r = post(service.client, request())
    assert r.status_code == 502 and "Could not get an answer" in r.json()["error"]
    assert service.store.spent(answer_service.periods()[1]) == pytest.approx(0.0)


@pytest.mark.parametrize(
    "body, problem",
    [
        (request(install="CS-OOOO-1111-2222"), "install_id"),  # O is not in the alphabet
        (request(question=""), "question"),
        (request(question="x" * 501), "question"),
        (request(entries=["a"] * 9), "entries"),
        (request(entries=["a" * 48_001]), "entries"),
        ([], "JSON object"),
    ],
)
def test_bad_requests_are_refused_before_anything_is_counted(service, body, problem):
    r = post(service.client, body)
    assert r.status_code == 400 and problem in r.json()["error"]
    assert service.claude.calls == [] and service.store.installs == {}


def test_big_or_unsized_bodies_are_refused(service):
    r = service.client.post("/v1/answer", content=b"x" * 200_001, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert service.client.get("/healthz").text == "ok"
    assert service.client.get("/v1/answer").status_code == 405


def test_parse_raised():
    assert parse_raised(f"{ME}=500 {OTHER}=1000") == {ME: 500, OTHER: 1000}
    assert parse_raised("") == {}
    assert parse_raised(f"{ME}=lots") == {}


def test_reservation_settles_the_difference():
    store = MemoryStore()
    ledger = QuotaLedger(store, ME, "203.0.113.7", Limits(daily=5, ip_daily=5, cap_usd=1.0, raised={}), now=0)
    r = ledger.reserve(0.01)
    assert store.spent("1970-01") == pytest.approx(0.01)
    r.settle(0.0003)
    assert store.spent("1970-01") == pytest.approx(0.0003)
    with pytest.raises(Refused):
        QuotaLedger(store, ME, "x", Limits(daily=1, ip_daily=5, cap_usd=1.0, raised={}), now=0).reserve(0.01)


# -- The app's side --------------------------------------------------------------------------------


def test_install_id_is_made_once_and_kept():
    first = online.install_id()
    assert online.is_install_id(first) and settings.load().install_id == first
    assert online.install_id() == first
    assert not online.is_install_id("CS-ILOU-0000-0000") and not online.is_install_id(None)


class FakeService(BaseHTTPRequestHandler):
    """The answer service as the app sees it: a canned reply per test."""

    reply = (200, b"")
    seen: list = []

    def do_POST(self):
        self.seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        status, body = self.reply
        self.send_response(status)
        self.send_header("Content-Type", "application/x-ndjson" if status == 200 else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_service(monkeypatch):
    handler = type("Svc", (FakeService,), {"seen": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("CIVSPEAK_ANSWER_URL", f"http://127.0.0.1:{httpd.server_address[1]}/")
    yield handler
    httpd.shutdown()
    httpd.server_close()


def ndjson(*messages):
    return b"".join(json.dumps(m).encode() + b"\n" for m in messages)


def test_client_streams_an_answer(fake_service):
    fake_service.reply = (200, ndjson({"delta": "Flat "}, {"delta": "land."}, {"done": {"text": "Flat land.", "used": [1, 7]}}))
    out = list(online.AnswerService().stream("where?", ["Campus"], spoken=True))
    assert out[:2] == [("delta", "Flat "), ("delta", "land.")]
    assert out[-1][1].text == "Flat land." and out[-1][1].used == [1]  # 7 is not one of the entries
    sent = fake_service.seen[0]
    assert sent["install_id"] == online.install_id() and sent["entries"] == ["Campus"] and sent["spoken"] is True


@pytest.mark.parametrize(
    "reply, raised",
    [
        ((429, b'{"limit": "daily", "allowed": 100}'), "daily"),
        ((429, b'{"limit": "monthly", "allowed": 100}'), "monthly"),
        ((502, b'{"error": "upstream"}'), "having trouble"),
        ((200, ndjson({"delta": "Half"})), "stopped part way"),
        ((200, ndjson({"error": "Could not get an answer."})), "Could not get an answer."),
    ],
)
def test_client_refusals(fake_service, reply, raised):
    fake_service.reply = reply
    if raised in ("daily", "monthly"):
        with pytest.raises(online.LimitReached) as e:
            list(online.AnswerService().stream("q", []))
        assert e.value.kind == raised and e.value.install_id == online.install_id()
        assert e.value.allowed == 100
    else:
        with pytest.raises(AnswerError, match=raised):
            list(online.AnswerService().stream("q", []))


def test_unreachable_service_says_so():
    with pytest.raises(AnswerError, match="internet connection"):
        list(online.AnswerService().stream("q", []))


def test_own_key_answers_directly_without_the_service(tmp_path):
    key = tmp_path / "anthropic-key"
    key.write_text("own-key-not-real\n")
    sent = []
    svc = SimpleNamespace(stream=lambda *a: sent.append(a) or iter(()), warm_up=lambda: None)
    a = Answerer(spend.Ledger(tmp_path / "spend.jsonl"), key, service=svc)
    a._client_for = lambda k: FakeClaude()
    hit = Hit({"title": "Campus", "section": "Districts", "group": "", "facts": [], "chapters": []}, 1.0)
    assert list(a.stream("q", [hit]))[-1][1].text == "Build it on flat land."
    assert sent == []
    key.unlink()
    list(a.stream("q", [hit]))
    assert sent and sent[0][1] == [answer.entry_text(hit.page)]


def test_limit_reaches_the_page_and_is_kept_for_a_page_opened_later(built_index, fake_service):
    fake_service.reply = (429, b'{"limit": "daily", "allowed": 100}')
    out, _ = built_index
    state = server.State(out.parent, Answerer(spend.Ledger(out.parent / "spend.jsonl"), out.parent / "no-key"))
    state.check = lambda: None
    events = []
    server.Answering(state, lambda name, data: events.append((name, data))).run("campus", None, spoken=False)
    kind, data = events[-1]
    assert kind == "problem" and data["kind"] == "limit" and data["limit"] == "daily"
    assert data["install_id"] == online.install_id() and data["email"] == server.LIMIT_EMAIL and data["allowed"] == 100
    assert "free limit of 100 questions" in data["message"]
    assert state.status()["limit"]["install_id"] == online.install_id()
    assert state.status()["own_key"] is False
    assert state.settings_view()["install_id"] == online.install_id()


def test_waking_the_service_pings_it_at_most_every_few_minutes(fake_service, monkeypatch):
    pinged = []
    svc = online.AnswerService()
    monkeypatch.setattr(svc, "_ping", lambda: pinged.append(1))
    monkeypatch.setattr(online.threading, "Thread", lambda target, **kw: SimpleNamespace(start=target))
    svc.warm_up()
    svc.warm_up()
    assert pinged == [1]
