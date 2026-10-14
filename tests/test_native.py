"""The Windows app's own microphone and speakers: when a spoken question is over, George played in
order and cut off, and the whole spoken question answered with no page open. A fake sounddevice
stands in for PortAudio, and fake Whisper, Kokoro and Claude for the models."""

import json
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from civspeak import audio, conversation, server, spend
from civspeak.answer import Answerer
from civspeak.hearing import read_wav
from civspeak.speech import to_wav
from test_answer_server import FakeClient
from test_voice import FakeEars, FakeVoice


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def tone(seconds, level=0.1, rate=16000):
    return (np.sin(np.arange(int(seconds * rate)) / 3) * level * np.sqrt(2)).astype(np.float32)


def quiet(seconds, rate=16000):
    return np.full(int(seconds * rate), 0.001, dtype=np.float32)


def feed(recording, samples):
    """Hand samples over a block at a time, as the microphone does; True once the question is over."""
    block = int(recording.rate * audio.BLOCK_SECONDS)
    return any([recording.add(samples[i : i + block]) for i in range(0, len(samples), block)])


# -- when a spoken question is over ------------------------------------------------------------


def test_a_tapped_question_ends_when_the_talking_stops():
    r = audio.Recording("toggle")
    assert not feed(r, quiet(0.5)) and not feed(r, tone(1.0)) and not feed(r, quiet(1.0))
    assert feed(r, quiet(0.3)) and r.heard and not r.silent  # 1.2 s of quiet after talking
    assert abs(r.seconds - 2.8) < 0.01


def test_a_tapped_question_gives_up_when_nobody_talks():
    r = audio.Recording("toggle")
    assert not feed(r, quiet(5.9)) and feed(r, quiet(0.1)) and not r.heard


def test_a_quiet_microphone_in_a_quiet_room_still_hears_talking():
    r = audio.Recording("toggle")
    assert not feed(r, quiet(0.5) / 50) and not feed(r, tone(1.0, level=0.004))  # too soft for the page's level
    assert r.heard and not feed(r, quiet(1.1) / 50) and feed(r, quiet(0.1) / 50)
    loud_room = audio.Recording("toggle")
    feed(loud_room, quiet(0.5) * 5)
    assert loud_room.speech_level == audio.SPEECH_LEVEL  # never needs more than the page did


def test_a_held_question_lasts_until_let_go_or_fifteen_seconds():
    r = audio.Recording("hold")
    assert not feed(r, quiet(8)) and not feed(r, tone(6.9))
    assert feed(r, quiet(0.1))


def test_the_microphone_level_and_the_recording():
    r = audio.Recording("hold", rate=48000)
    r.add(tone(0.1, level=0.04, rate=48000))
    assert abs(r.level - 0.5) < 0.02
    r.add(tone(0.1, level=0.5, rate=48000))
    assert r.level == 1.0
    samples = read_wav(r.wav())  # Whisper gets 16 kHz whatever the microphone gave
    assert abs(len(samples) - 3200) <= 1


def test_nothing_but_zeros_means_windows_blocked_the_microphone():
    r = audio.Recording("hold")
    r.add(np.zeros(1600, dtype=np.float32))
    assert r.silent and not r.heard
    r.add(quiet(0.1))
    assert not r.silent


# -- a fake sounddevice -----------------------------------------------------------------------


class PortAudioError(Exception):
    pass


class FakeStream:
    def __init__(self, sd, kind, samplerate, callback=None, write_seconds=0.0):
        self.sd, self.kind, self.samplerate, self.callback = sd, kind, samplerate, callback
        self.written = []
        self.events = []
        self.write_seconds = write_seconds

    def start(self):
        self.events.append("start")

    def write(self, samples):
        time.sleep(self.write_seconds)
        self.written.append(samples)

    def send(self, samples):
        """The microphone hands over a block (on PortAudio's thread in real life)."""
        self.callback(samples.reshape(-1, 1), len(samples), None, None)

    def stop(self):
        self.events.append("stop")

    def abort(self):
        self.events.append("abort")

    def close(self):
        self.events.append("close")


class FakeSD:
    PortAudioError = PortAudioError

    def __init__(self, microphone=True, input_rates=(16000, 48000), output_rates=(24000, 48000), write_seconds=0.0):
        self.microphone = microphone
        self.input_rates, self.output_rates = input_rates, output_rates
        self.write_seconds = write_seconds
        self.streams: list[FakeStream] = []
        self.restarts = 0
        self.plugged_in_later = False

    def query_devices(self, kind):
        if kind == "input" and not self.microphone:
            raise PortAudioError("Error querying device -1")
        return {"default_samplerate": 48000.0}

    def _terminate(self):
        pass

    def _initialize(self):
        self.restarts += 1
        if self.plugged_in_later:
            self.microphone = True

    def InputStream(self, samplerate, channels, dtype, blocksize, callback):
        assert (channels, dtype, blocksize) == (1, "float32", int(samplerate * audio.BLOCK_SECONDS))
        if not self.microphone or samplerate not in self.input_rates:
            raise PortAudioError("Invalid sample rate")
        self.streams.append(FakeStream(self, "input", samplerate, callback))
        return self.streams[-1]

    def OutputStream(self, samplerate, channels, dtype):
        assert (channels, dtype) == (1, "int16")
        if samplerate not in self.output_rates:
            raise PortAudioError("Invalid sample rate")
        self.streams.append(FakeStream(self, "output", samplerate, write_seconds=self.write_seconds))
        return self.streams[-1]

    def of(self, kind):
        return [s for s in self.streams if s.kind == kind]


def test_no_microphone_and_one_plugged_in_later():
    sd = FakeSD(microphone=False)
    sound = audio.Sound(sd)
    assert sound.problem() == audio.NO_MICROPHONE and sd.restarts == 1  # the device list was read again
    sd.plugged_in_later = True
    assert sound.problem() == "" and sd.restarts == 2


def test_without_sounddevice_the_microphone_says_so(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", None)
    assert audio.Sound().problem() == audio.NO_MODULE


def test_the_microphone_records_at_16_khz_or_else_its_own_rate():
    sd = FakeSD()
    mic = audio.Microphone(audio.Sound(sd))
    got = []
    assert mic.open(got.append) == 16000
    sd.of("input")[0].send(np.ones(1600, dtype=np.float32))
    assert len(got) == 1 and got[0].shape == (1600,)
    mic.close()
    assert sd.of("input")[0].events == ["start", "abort", "close"]

    sd.input_rates = (48000,)
    assert audio.Microphone(audio.Sound(sd)).open(got.append) == 48000


# -- George out of the speakers ----------------------------------------------------------------


def clip(seconds, rate=24000):
    from concurrent.futures import Future

    f = Future()
    f.set_result(to_wav(np.full(int(seconds * rate), 0.25, dtype=np.float32), rate))
    return f


def test_sentences_play_in_order_then_the_speakers_close():
    sd = FakeSD()
    said = []
    speakers = audio.Speakers(audio.Sound(sd), said.append)
    turn = speakers.begin()
    speakers.play(clip(0.2), turn)
    speakers.play(clip(0.1), turn)
    wait_for(lambda: said == [True, False])
    (out,) = sd.of("output")
    assert out.samplerate == 24000 and sum(len(w) for w in out.written) == 7200
    assert all(len(w) <= 1200 for w in out.written)  # 50 ms at a time, so Esc is quick
    assert out.events == ["start", "stop", "close"]  # the last sentence played out before closing


def test_stopping_cuts_george_off_and_drops_the_rest():
    sd = FakeSD(write_seconds=0.01)
    said = []
    speakers = audio.Speakers(audio.Sound(sd), said.append)
    turn = speakers.begin()
    for _ in range(3):
        speakers.play(clip(1.0), turn)
    wait_for(lambda: said == [True])
    speakers.stop()
    wait_for(lambda: said == [True, False])
    (out,) = sd.of("output")
    assert out.events == ["start", "abort", "close"]
    assert sum(len(w) for w in out.written) < 24000  # not even the first sentence finished
    speakers.play(clip(0.1), turn)  # a late sentence of the stopped answer is never played
    time.sleep(0.2)
    assert len(sd.of("output")) == 1


def test_speakers_that_refuse_kokoros_rate_get_it_resampled():
    sd = FakeSD(output_rates=(48000,))
    said = []
    speakers = audio.Speakers(audio.Sound(sd), said.append)
    speakers.play(clip(0.1), speakers.begin())
    wait_for(lambda: said == [True, False])
    (out,) = sd.of("output")
    assert out.samplerate == 48000 and sum(len(w) for w in out.written) == 4800


# -- a spoken question answered with no page open --------------------------------------------


class FakeTalk:
    """Every open page: collects what they are told."""

    def __init__(self):
        self.events = []

    def broadcast(self, event, data):
        self.events.append((event, data))

    def kinds(self):
        return [e for e, _ in self.events]

    def last(self, kind):
        return [d for e, d in self.events if e == kind][-1]


class WavVoice(FakeVoice):
    def synthesize(self, sentence):
        self.said.append(sentence)
        return to_wav(np.zeros(240, dtype=np.float32), 24000)


@pytest.fixture
def talking(built_index, tmp_path):
    """A Conversation on a fake microphone, with fake Whisper, George and Claude."""
    out, _ = built_index
    key = tmp_path / "key"
    key.write_text("test-key-not-real")
    clock = [100.0]

    def make(text="Where can I build a Thinkery?", sd=None, key_path=key):
        answerer = Answerer(spend.Ledger(tmp_path / "spend.jsonl"), key_path)
        client = FakeClient(["Put it on Meadow. ", "It gives Science.", "\nUSED: 1"])
        answerer._client_for = lambda k: client
        state = server.State(out.parent, answerer, voice=WavVoice(), ears=FakeEars(text))
        state.check = lambda: None
        state.wake = lambda: None
        sd = sd or FakeSD()
        talk = FakeTalk()
        c = conversation.Conversation(state, talk, audio.Sound(sd), clock=lambda: clock[0])
        return types.SimpleNamespace(c=c, sd=sd, talk=talk, state=state, client=client, clock=clock)

    return make


def say_into(t, samples, n=0):
    """The microphone (the n-th opened) hears `samples`, and the conversation has taken them."""
    levels = t.talk.kinds().count("level")
    t.sd.of("input")[n].send(samples)
    wait_for(lambda: t.talk.kinds().count("level") > levels)


def mic_states(talk):
    return [d["state"] for e, d in talk.events if e == "mic"]


def test_hold_the_key_ask_and_let_go(talking):
    t = talking()
    assert t.c.press("down")
    (mic,) = t.sd.of("input")
    for _ in range(9):
        say_into(t, tone(0.1))
    mic.send(tone(0.1))  # the last moment before the key is let go is kept too
    t.clock[0] += 1.0
    assert t.c.press("up")
    wait_for(lambda: "done" in t.talk.kinds())
    assert mic.events == ["start", "abort", "close"]  # the microphone is only open while you ask
    assert mic_states(t.talk) == ["listening", "working", "idle"]
    kinds = t.talk.kinds()
    assert kinds.index("question") < kinds.index("entries") < kinds.index("delta") < kinds.index("done")
    assert t.talk.last("question") == {"text": "Where can I build a Thinkery?"}
    assert len(read_wav(t.state.ears.got[0])) == 16000  # Whisper got the whole second
    assert "spoken aloud" in t.client.calls[0]["messages"][0]["content"]
    wait_for(lambda: t.talk.kinds().count("speaking") == 2)
    assert t.state.voice.said == ["Put it on Meadow.", "It gives Science."]
    assert [d for e, d in t.talk.events if e == "speaking"] == [{"on": True}, {"on": False}]
    assert sum(len(w) for w in t.sd.of("output")[0].written) == 480  # both sentences out of the speakers
    assert t.state.last_listen["audio_s"] == 1.0  # timings paired with the answer as for the page


def test_a_tap_listens_until_the_talking_stops(talking):
    t = talking()
    t.c.press("down")
    t.clock[0] += 0.1
    t.c.press("up")
    assert t.talk.events[-1] == ("mic", {"state": "listening", "mode": "toggle"})
    (mic,) = t.sd.of("input")
    for samples in (tone(1.0), quiet(1.2)):
        for i in range(0, len(samples), 1600):
            mic.send(samples[i : i + 1600])
    wait_for(lambda: "done" in t.talk.kinds())
    assert mic_states(t.talk) == ["listening", "listening", "working", "idle"]


def test_tapping_again_finishes_and_esc_cancels(talking):
    t = talking()
    t.c.press("down")
    t.clock[0] += 0.1
    t.c.press("up")
    say_into(t, tone(0.5))
    t.c.press("down")  # the second tap ends the question
    wait_for(lambda: "done" in t.talk.kinds())

    t.c.press("down")
    t.c.press("cancel")
    assert mic_states(t.talk)[-2:] == ["listening", "idle"]
    assert len(t.state.ears.got) == 1  # the cancelled one was never written down


def test_nothing_said_and_nothing_caught_are_said_out_loud(talking):
    t = talking(text="")
    t.c.press("down")
    t.clock[0] += 0.1
    t.c.press("up")
    mic = t.sd.of("input")[0]
    for _ in range(60):
        mic.send(quiet(0.1))
    wait_for(lambda: "problem" in t.talk.kinds())
    assert t.talk.last("problem")["message"] == conversation.NOTHING_HEARD and not t.state.ears.got
    wait_for(lambda: t.state.voice.said == [server.NOT_HEARD])

    t.c.press("down")
    say_into(t, tone(0.5), n=1)
    t.clock[0] += 1
    t.c.press("up")
    wait_for(lambda: len(t.state.voice.said) == 2)
    assert t.talk.last("problem")["message"] == conversation.NOT_CAUGHT and "question" not in t.talk.kinds()


def test_a_microphone_windows_blocks_is_explained(talking):
    t = talking()
    t.c.press("down")
    say_into(t, np.zeros(1600, dtype=np.float32))
    t.clock[0] += 1
    t.c.press("up")
    wait_for(lambda: "problem" in t.talk.kinds())
    assert t.talk.last("problem")["message"] == audio.MUTED and not t.state.ears.got


def test_with_no_microphone_the_key_beeps_and_the_page_says_why(talking):
    t = talking(sd=FakeSD(microphone=False))
    assert t.c.problem() == audio.NO_MICROPHONE
    assert t.c.press("down") is False
    assert t.talk.events == [("problem", {"kind": "mic", "message": audio.NO_MICROPHONE})]


def test_voice_off_answers_without_george(talking):
    t = talking()
    t.c.speak = False
    t.c.press("down")
    say_into(t, tone(0.5))
    t.clock[0] += 1
    t.c.press("up")
    wait_for(lambda: "done" in t.talk.kinds())
    assert t.state.voice.said == [] and "say" not in t.talk.kinds() and not t.sd.of("output")


def test_a_typed_question_quiets_the_spoken_answer(talking):
    t = talking()
    gate = threading.Event()
    real = t.state.hear

    def slow_hear(wav):
        gate.wait(5)
        return real(wav)

    t.state.hear = slow_hear
    t.c.press("down")
    say_into(t, tone(0.5))
    t.clock[0] += 1
    t.c.press("up")
    typed = []
    speaker = t.c.typed(lambda name, data: typed.append(name), speak=True)
    gate.set()
    wait_for(lambda: mic_states(t.talk)[-1] == "idle")
    time.sleep(0.1)
    assert "question" not in t.talk.kinds()  # written down, but the typed question came first
    assert speaker is t.c.speaker and speaker.speakers is t.c.speakers

    t.c.hush()
    assert speaker.cancelled


# -- the page's side --------------------------------------------------------------------------


@pytest.fixture
def app_server(talking):
    t = talking()
    t.c.talk = server.Talk()
    servers = []

    def start(native=True):
        handler = type("H", (server.Handler,), {"state": t.state, "talk": t.c.talk, "conversation": t.c if native else None})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", t

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def post(url, path, payload, content_type="application/json"):
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(), headers={"Content-Type": content_type})
    return urllib.request.urlopen(req, timeout=10)


def test_the_page_drives_the_apps_microphone_and_voice(app_server):
    url, t = app_server()
    status = json.load(urllib.request.urlopen(url + "/api/status", timeout=5))
    assert status["native"] is True and status["mic"]["problem"] == ""
    assert json.load(post(url, "/api/voice", {"on": False})) == {"ok": True} and t.c.speak is False
    post(url, "/api/mic", {"key": "down"})
    assert t.c.mic_state == "listening"
    post(url, "/api/mic", {"key": "cancel"})
    assert t.c.mic_state == "idle"
    for payload, ctype, code in [({"key": "sideways"}, "application/json", 400), ({"key": "down"}, "text/plain", 415)]:
        with pytest.raises(urllib.error.HTTPError) as e:
            post(url, "/api/mic", payload, ctype)
        assert e.value.code == code


def test_a_typed_question_is_spoken_out_of_the_apps_speakers(app_server):
    url, t = app_server()
    body = post(url, "/api/ask", {"question": "thinkery", "speak": True}).read().decode()
    assert "event: done" in body
    wait_for(lambda: t.sd.of("output") and sum(len(w) for w in t.sd.of("output")[0].written) == 480)
    post(url, "/api/hush", {})


def test_without_the_app_the_page_records_itself(app_server):
    url, _ = app_server(native=False)
    status = json.load(urllib.request.urlopen(url + "/api/status", timeout=5))
    assert status["native"] is False
    for path in ("/api/mic", "/api/hush", "/api/voice"):
        with pytest.raises(urllib.error.HTTPError) as e:
            post(url, path, {"key": "down"})
        assert e.value.code == 404
