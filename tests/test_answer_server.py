"""Answer streaming, key handling, spend settlement and the local server, with a fake Claude."""

import json
import os
import stat
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from civspeak import answer, server, spend
from civspeak.answer import Answerer, KeyMissing, SpokenFilter, read_key, split_used
from civspeak.search import Hit


class FakeStream:
    def __init__(self, chunks, final):
        self.text_stream = iter(chunks)
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class FakeClient:
    def __init__(self, chunks, usage=None, stop_reason="end_turn"):
        self.calls = []
        text = "".join(chunks)
        usage = usage or SimpleNamespace(
            input_tokens=1000, output_tokens=100, cache_read_input_tokens=800, cache_creation_input_tokens=0, iterations=None
        )
        final = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            usage=usage,
            model=answer.MODEL,
            stop_reason=stop_reason,
        )
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self._chunks, self._final = chunks, final

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self._chunks, self._final)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "config" / "anthropic-key"
    path.parent.mkdir()
    path.write_text("test-key-not-real\n")
    os.chmod(path, 0o644)
    return path


def hit(title="Thinkery"):
    return Hit({"title": title, "section": "Districts", "group": "", "facts": [], "chapters": []}, 1.0)


def test_key_is_read_from_the_file_only_and_tightened(key_file, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-environment-key-that-must-be-ignored")
    assert read_key(key_file) == "test-key-not-real"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    key_file.write_text("  \n")
    with pytest.raises(KeyMissing):
        read_key(key_file)
    with pytest.raises(KeyMissing):
        read_key(key_file.parent / "nope")


def test_used_line_is_never_spoken():
    f = SpokenFilter()
    out = "".join(f.feed(c) for c in ["The Thinkery ", "gives Science.\nUS", "ED: 1, 2"])
    assert out == "The Thinkery gives Science."
    assert split_used("Answer.\nUSED: 1, 3") == ("Answer.", [1, 3])
    assert split_used("Answer.\nUSED: none") == ("Answer.", [])


def test_markdown_emphasis_never_shows(tmp_path, key_file):
    a = Answerer(spend.Ledger(tmp_path / "spend.jsonl"), key_file)
    a._client_for = lambda key: FakeClient(["**The Thinkery** goes", " on *Meadow*.", "\nUSED: 1"])
    events = list(a.stream("q", [hit()]))
    assert "".join(v for k, v in events if k == "delta") == "The Thinkery goes on Meadow."
    assert events[-1][1].text == "The Thinkery goes on Meadow."


def test_stream_request_shape_cost_and_settlement(tmp_path, key_file):
    ledger = spend.Ledger(tmp_path / "spend.jsonl")
    a = Answerer(ledger, key_file)
    fake = FakeClient(["The Thinkery goes on Meadow.", "\nUSED: 1"])
    a._client_for = lambda key: fake
    events = list(a.stream("where does a thinkery go", [hit()]))
    deltas = "".join(v for k, v in events if k == "delta")
    result = events[-1][1]
    assert deltas == "The Thinkery goes on Meadow."
    assert result.text == "The Thinkery goes on Meadow." and result.used == [1]
    expected = spend.cost_usd(answer.MODEL, 1000, 100, 800, 0)
    assert result.cost_usd == pytest.approx(expected)
    assert ledger.month_total() == pytest.approx(expected)

    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"] == {"effort": "low"}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in call  # cannot be disabled on Opus 5.5; effort controls depth
    assert '<entry number="1">' in call["messages"][0]["content"]
    assert call["messages"][0]["content"].endswith("Question: where does a thinkery go")


def test_system_prompt_is_long_enough_to_cache():
    # Opus 5.5 only caches prefixes of at least 512 tokens (~4 characters per token).
    assert len(answer.SYSTEM_PROMPT) / 4 > 600


def test_cap_blocks_before_any_call(tmp_path, key_file):
    ledger = spend.Ledger(tmp_path / "spend.jsonl", cap=0.01)
    a = Answerer(ledger, key_file)
    a._client_for = lambda key: pytest.fail("must not call Claude over the cap")
    with pytest.raises(spend.CapReached):
        list(a.stream("q", [hit()]))


@pytest.fixture
def running(built_index, tmp_path, key_file):
    out, _ = built_index

    def start(answerer):
        state = server.State(out, answerer)
        state.check = lambda: None
        handler = type("H", (server.Handler,), {"state": state, "talk": server.Talk()})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True  # open event streams must not hold up shutdown
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"

    servers = []
    yield lambda answerer: servers.append(start(answerer)) or servers[-1][1]
    for httpd, _ in servers:
        httpd.shutdown()
        httpd.server_close()


def ask(url, question):
    req = urllib.request.Request(
        f"{url}/api/ask",
        data=json.dumps({"question": question}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    body = urllib.request.urlopen(req, timeout=10).read().decode()
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_page_and_answer_stream(running, tmp_path, key_file):
    a = Answerer(spend.Ledger(tmp_path / "spend.jsonl"), key_file)
    a._client_for = lambda key: FakeClient(["Put it on Meadow.", "\nUSED: 1"])
    url = running(a)
    page = urllib.request.urlopen(url + "/", timeout=5).read().decode()
    assert "Civspeak" in page and "/app.js" in page
    events = ask(url, "where can I build a thinkery")
    kinds = [k for k, _ in events]
    assert kinds[0] == "entries" and kinds[-1] == "done"
    assert events[0][1][0]["title"] == "Thinkery"
    assert events[-1][1]["text"] == "Put it on Meadow." and events[-1][1]["used"] == [1]


def test_no_key_still_shows_entries(running, tmp_path):
    url = running(Answerer(spend.Ledger(tmp_path / "spend.jsonl"), tmp_path / "missing-key"))
    events = ask(url, "thinkery")
    assert events[0][0] == "entries" and events[0][1]
    assert events[-1][0] == "problem" and events[-1][1]["kind"] == "no_key"
    assert "No Claude API key" in events[-1][1]["message"]


def test_monthly_limit_is_explained(running, tmp_path, key_file):
    ledger = spend.Ledger(tmp_path / "spend.jsonl", cap=0.0)
    url = running(Answerer(ledger, key_file))
    events = ask(url, "thinkery")
    assert events[-1][0] == "problem" and events[-1][1]["kind"] == "cap"
    assert "Monthly limit reached" in events[-1][1]["message"]


def test_status_and_non_json_posts_are_refused(running, tmp_path, key_file):
    url = running(Answerer(spend.Ledger(tmp_path / "spend.jsonl"), key_file))
    status = json.loads(urllib.request.urlopen(url + "/api/status", timeout=5).read())
    assert status["pages"] == 6 and status["key"] in (True, False) and status["cap"] == 10.0
    req = urllib.request.Request(
        f"{url}/api/ask", data=b"question=x", headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 415
