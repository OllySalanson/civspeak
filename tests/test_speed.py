"""Staying light and quick: idle unload, waking on the talk key, the latency log, CPU settings."""

import json
import threading
import time
import urllib.request

from civspeak import idle, latency, paths, server
from civspeak.loc import Localizer
from civspeak.search import entry_text
from test_voice import FakeEars, FakeVoice, post_json, voiced  # noqa: F401  (voiced is a fixture)


class Model:
    def __init__(self, last_used):
        self.loaded = True
        self.last_used = last_used
        self.unloads = 0

    def unload(self):
        self.loaded = False
        self.unloads += 1
        return True


def test_models_unload_only_after_being_idle_long_enough():
    now = [1000.0]
    fresh, stale = Model(990.0), Model(100.0)
    unloader = idle.IdleUnloader([fresh, stale], after=600, clock=lambda: now[0])
    assert unloader.check() == [stale]
    assert fresh.loaded and not stale.loaded
    assert unloader.check() == []  # already unloaded
    now[0] = 1600.0
    assert unloader.check() == [fresh]
    assert idle.IdleUnloader([Model(0)], after=0).check  # 0 means never: start() does not run it


def test_voice_unloads_and_loads_again_when_next_used():
    class CountingVoice(FakeVoice):
        def __init__(self):
            super().__init__()
            self.loads = 0

        @property
        def loaded(self):
            return self._kokoro is not None

        def _load_locked(self):
            self.loads += 1
            self._kokoro = object()

        def load(self):
            self._acquire()

    v = CountingVoice()
    v.load()
    assert v.loaded and v.unload() and not v.loaded
    assert v.unload() is False
    v.load()
    assert v.loads == 2


def test_cpu_and_idle_settings(monkeypatch):
    assert paths.cpu_threads() == 4
    monkeypatch.setenv("CIVSPEAK_THREADS", "2")
    assert paths.cpu_threads() == 2
    monkeypatch.setenv("CIVSPEAK_THREADS", "99")
    assert paths.cpu_threads() == 8
    assert paths.idle_unload_seconds() == 1200
    monkeypatch.setenv("CIVSPEAK_IDLE_MINUTES", "0")
    assert paths.idle_unload_seconds() == 0


class WakingVoice(FakeVoice):
    def __init__(self):
        super().__init__()
        self.is_loaded = False

    @property
    def loaded(self):
        return self.is_loaded


def test_the_talk_key_wakes_unloaded_models(voiced, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server.Handler, "talk", server.Talk(), raising=False)
    url, state, _ = voiced()
    state.voice = WakingVoice()
    state.narrator.voice = state.voice
    woken = threading.Event()
    state.ears = FakeEars()
    state.ears.warm_up = lambda index=None: woken.set()
    req = urllib.request.Request(
        url + "/api/hotkey", data=b'{"key": "down"}', headers={"Content-Type": "application/json"}, method="POST"
    )
    assert json.loads(urllib.request.urlopen(req, timeout=5).read()) == {"listening": False}
    assert woken.wait(5)  # Whisper is loading in the background
    deadline = time.monotonic() + 5
    while "Ready." not in state.voice.said and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "Ready." in state.voice.said  # George was loaded and run once


def test_each_question_is_timed_without_its_words(voiced, tmp_path):  # noqa: F811
    url, state, _ = voiced()
    state.latency = latency.LatencyLog(tmp_path / "latency.jsonl")
    state.last_listen = {"at": time.monotonic(), "listen_s": 1.25, "audio_s": 2.0, "ears_cold": False}
    post_json(url, "/api/ask", {"question": "where can I build a thinkery", "speak": True, "spoken": True})
    [entry] = state.latency.recent()
    assert entry["outcome"] == "answered" and entry["spoken"] is True
    assert entry["listen_s"] == 1.25 and entry["first_voice_s"] >= 0 and "first_text_s" in entry
    assert "thinkery" not in json.dumps(entry).lower()
    assert latency.first_words_seconds(entry) == round(entry["first_voice_s"] + 1.25, 2)
    assert latency.summary([entry]).startswith("first words after")
    assert latency.summary([]) == "no questions timed yet"


def test_latency_log_stays_small(tmp_path, monkeypatch):
    monkeypatch.setattr(latency, "KEEP", 5)
    log = latency.LatencyLog(tmp_path / "latency.jsonl")
    for i in range(12):
        log.record({"total_s": i})
    lines = (tmp_path / "latency.jsonl").read_text().splitlines()
    assert 5 <= len(lines) < 10 and json.loads(lines[-1])["total_s"] == 11


def test_named_placeholders_like_modifier_arguments():
    loc = Localizer({"LOC_X": "Gain {Amount : number #} Gold and {1_Thing}."})
    assert loc.lookup("LOC_X", "a hat", named={"Amount": 30}) == "Gain 30 Gold and a hat."
    assert loc.lookup("LOC_X", "a hat") == "Gain [value] Gold and a hat."


def test_long_fact_lists_are_cut_to_the_prompt_budget():
    page = {
        "title": "Every Adjacency",
        "section": "Districts",
        "group": "",
        "facts": [{"heading": f"District {i}", "lines": ["+1 Science from each adjacent thing tile."] * 20} for i in range(40)],
        "chapters": [],
    }
    assert len(entry_text(page, budget=3000)) < 3100
