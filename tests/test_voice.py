"""George speaking and the microphone: sentence splitting, the Whisper hint list, recordings, and the
server's speech and listen endpoints, with fakes standing in for Kokoro, Whisper and Claude."""

import io
import json
import threading
import time
import urllib.error
import urllib.request
import wave
from concurrent.futures import Future
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from civspeak import server, spend
from civspeak.answer import Answerer, build_user_message
from civspeak.hearing import BadAudio, Ears, Heard, clean_transcript, hint_kind, hint_texts, read_wav
from civspeak.search import STOPWORDS, Index
from civspeak.speech import Narrator, SentenceSplitter, Voice, speech_text, to_wav
from test_answer_server import FakeClient, ask


def split_stream(chunks):
    s = SentenceSplitter()
    out = []
    for c in chunks:
        out += s.feed(c)
    return out, s.flush()


def test_sentences_come_out_as_soon_as_the_next_one_starts():
    s = SentenceSplitter()
    assert s.feed("The Thinkery gives Science.") == []  # might still be "Science.com" or "e.g."
    assert s.feed(" It goes") == ["The Thinkery gives Science."]
    assert s.feed(" on Meadow, e.g. near hills. Costs 54!") == ["It goes on Meadow, e.g. near hills."]
    assert s.flush() == ["Costs 54!"]
    assert s.flush() == []


def test_sentences_survive_odd_streaming_boundaries():
    whole, rest = split_stream(["Yes", ". ", "It costs 1", ".5 Gold", " per turn. Done"])
    assert whole == ["Yes.", "It costs 1.5 Gold per turn."] and rest == ["Done"]
    whole, rest = split_stream(['He said "Go." then left. And', " more"])
    assert whole == ['He said "Go." then left.'] and rest == ["And more"]


def test_a_long_first_sentence_starts_at_its_first_comma():
    whole, rest = split_stream(
        ["A Thinkery can go on any tile within 3 tiles", " of your City Center,", " but never", " on Lava. Its"]
    )
    assert whole == ["A Thinkery can go on any tile within 3 tiles of your City Center,", "but never on Lava."]
    # Only the first piece is cut early, and never at a short opening clause or inside a number.
    whole, _ = split_stream(["Yes, it can. It costs 1,000 Gold, which is a lot of Gold for one turn, so", " save up."])
    assert whole == ["Yes, it can."]


def test_speech_text_reads_symbols_as_words():
    assert (
        speech_text("Gives +2 [ICON_Science] Science and 50% more **Gold**.") == "Gives plus 2 Science and 50 percent more Gold."
    )
    assert speech_text("Faith & Culture") == "Faith and Culture"


def test_wav_round_trip_and_resampling():
    tone = np.sin(np.linspace(0, 200, 44100)).astype(np.float32) * 0.5
    data = to_wav(tone, 44100)
    samples = read_wav(data)
    assert samples.dtype == np.float32 and abs(len(samples) - 16000) <= 1
    # Stereo is mixed down.
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(3200, dtype="<i2").tobytes())
    assert len(read_wav(buf.getvalue())) == 1600
    with pytest.raises(BadAudio):
        read_wav(b"not a wav at all")


def test_clean_transcript_drops_whisper_filler():
    assert clean_transcript(" Where can I build a Thinkery? ") == "Where can I build a Thinkery?"
    assert clean_transcript("Thank you.") == ""
    assert clean_transcript("  ") == ""


# -- the Whisper hint list ------------------------------------------------------------------------

COMMON = {"campus", "harbor", "the", "of", "great", "lighthouse", "river", "archer"}


def fake_tokens(text: str) -> int:
    """Ordinary words are one token; made-up names are three."""
    return sum(1 if w.strip(",.:").lower() in COMMON else 3 for w in text.split()) + text.count(",")


def test_hint_list_prefers_civ_specific_names_and_fits_the_budget():
    pages = [
        ("DISTRICTS", "", "Campus", False),  # generic and ordinary English: never hinted
        ("DISTRICTS", "", "Thinkery", True),
        ("BUILDINGS", "", "Moon Garden", False),  # generic building: not hinted
        ("CIVILIZATIONS", "Civilizations", "Zorbania", False),
        ("CIVILIZATIONS", "Leaders", "Queen Ozma (Grand)", False),
        ("CIVILIZATIONS", "Leaders", "Queen Ozma (Wise)", False),  # the same name once
        ("WONDERS", "", "Great Lighthouse", False),  # every word ordinary: not hinted
        ("WONDERS", "", "Blorptower", False),
        ("UNITS", "", "Archer", False),
        ("UNITPROMOTIONS", "", "Zapper", False),  # not a kind people ask about by name
    ]
    hotwords, prompt = hint_texts(pages, fake_tokens, budget=1000)
    assert hotwords == "Thinkery, Zorbania, Queen Ozma, Blorptower" and prompt == ""

    many = [("DISTRICTS", "", f"Zib{n}", True) for n in range(30)] + [("WONDERS", "", f"Wox{n}", False) for n in range(30)]
    hotwords, prompt = hint_texts(many, fake_tokens, budget=20)
    first, second = hotwords.split(", "), prompt.rstrip(".").split(", ")
    assert len(first) == len(second) == 5  # 4 tokens a name ("Zib1,"), 20 tokens a list
    assert first[:4] == ["Zib0", "Zib1", "Zib2", "Wox0"]  # kinds take turns: 3 unique buildings, then 2 wonders
    assert prompt.endswith(".")


def test_hint_kinds():
    assert hint_kind("IMPROVEMENTS", "", True) == "unique infrastructure"
    assert hint_kind("UNITS", "", True) == "unique unit"
    assert hint_kind("UNITS", "", False) == ""
    assert hint_kind("CIVILIZATIONS", "Random Agendas", False) == ""


def test_index_lists_names_with_their_kind(built_index):
    out, _ = built_index
    names = Index(out).name_list()
    assert ("PLACES", "", "Thinkery", False) in names
    assert all(len(n) == 4 for n in names)


# -- search falls back to sound-alikes for misheard names -----------------------------------------


def test_misheard_name_is_found_by_its_sound(built_index):
    out, _ = built_index
    hits = Index(out).search("what does the handsel do")  # Whisper's guess at "Hanzel"
    assert hits[0].page["title"] == "Hanzel" and hits[0].why == ["sounds close to 'handsel'"]


def test_does_is_a_stopword_after_singular():
    assert "doe" in STOPWORDS


def test_spoken_questions_tell_claude_names_may_be_misheard():
    assert build_user_message("where is the handsel", [], spoken=True).endswith(
        "Question (spoken aloud and written down by speech recognition, so names may be misheard): where is the handsel"
    )
    assert build_user_message("x", []).endswith("\n\nQuestion: x")


# -- the narrator ---------------------------------------------------------------------------------


class FakeVoice(Voice):
    def __init__(self, delay=0.0):
        super().__init__()
        self.said = []
        self.delay = delay

    def problem(self):
        return ""

    @property
    def loaded(self):
        return True

    def load(self):
        pass

    def synthesize(self, sentence):
        time.sleep(self.delay)
        self.said.append(sentence)
        return b"RIFF" + sentence.encode()


def test_a_new_answer_cancels_what_is_still_queued():
    voice = FakeVoice(delay=0.5)  # "One." keeps the worker busy while the rest queue
    n = Narrator(voice)
    n.start("first")
    n.say("first", "One.")
    n.say("first", "Two.")
    n.start("second")
    n.say("second", "Three.")
    assert n.clip("second", 0).result(timeout=5) == b"RIFFThree."
    assert n.clip("first", 1).cancelled()
    assert "Two." not in voice.said
    assert n.clip("second", 5) is None and n.clip("nope", 0) is None


# -- the server -----------------------------------------------------------------------------------


class FakeEars(Ears):
    def __init__(self, text="Where can I build a Thinkery?"):
        super().__init__()
        self.text = text
        self.got = []

    def problem(self):
        return ""

    def transcribe(self, wav, index=None):
        read_wav(wav)
        self.got.append(wav)
        return Heard(self.text, 0.4, 1.0)


@pytest.fixture
def voiced(built_index, tmp_path):
    out, _ = built_index
    key = tmp_path / "key"
    key.write_text("test-key-not-real")
    servers = []

    def start(chunks=None, ears=None, key_path=key):
        a = Answerer(spend.Ledger(tmp_path / "spend.jsonl"), key_path)
        fake = FakeClient(chunks or ["Put it on Meadow. ", "It gives Science.", "\nUSED: 1"])
        a._client_for = lambda k: fake
        state = server.State(out, a, voice=FakeVoice(), ears=ears or FakeEars())
        state.check = lambda: None
        handler = type("H", (server.Handler,), {"state": state})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", state, fake

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def post_json(url, path, payload):
    req = urllib.request.Request(
        url + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    body = urllib.request.urlopen(req, timeout=10).read().decode()
    return [
        (dict(line.split(": ", 1) for line in b.splitlines())["event"], json.loads(b.split("data: ", 1)[1]))
        for b in body.strip().split("\n\n")
    ]


def listen(url, data, content_type="audio/wav", speak=True):
    req = urllib.request.Request(
        f"{url}/api/listen?speak={int(speak)}", data=data, headers={"Content-Type": content_type}, method="POST"
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get(url):
    return urllib.request.urlopen(url, timeout=10).read()


def test_answer_is_spoken_sentence_by_sentence(voiced):
    url, state, fake = voiced()
    events = post_json(url, "/api/ask", {"question": "where can I build a thinkery", "speak": True})
    says = [d for k, d in events if k == "say"]
    assert [s["text"] for s in says] == ["Put it on Meadow.", "It gives Science."]
    # The first sentence is queued for George before the answer has finished streaming.
    kinds = [k for k, _ in events]
    assert kinds.index("say") < kinds.index("done")
    assert [get(url + s["url"]) for s in says] == [b"RIFFPut it on Meadow.", b"RIFFIt gives Science."]
    assert "Question: where can I build a thinkery" in fake.calls[0]["messages"][0]["content"]


def test_no_speech_unless_asked(voiced):
    url, state, _ = voiced()
    events = post_json(url, "/api/ask", {"question": "thinkery"})
    assert "say" not in [k for k, _ in events]
    assert state.voice.said == []


def test_problems_are_spoken_too(voiced, tmp_path):
    url, _, _ = voiced(key_path=tmp_path / "missing")
    events = post_json(url, "/api/ask", {"question": "thinkery", "speak": True})
    assert events[-2][0] == "problem" and events[-1][0] == "say"
    assert events[-1][1]["text"] == server.SPOKEN_PROBLEMS["no_key"]


def test_spoken_question_is_marked_for_claude(voiced):
    url, _, fake = voiced()
    post_json(url, "/api/ask", {"question": "where is the handsel", "spoken": True})
    assert "spoken aloud" in fake.calls[0]["messages"][0]["content"]


def test_listen_transcribes_a_recording(voiced):
    ears = FakeEars()
    url, _, _ = voiced(ears=ears)
    reply = listen(url, to_wav(np.zeros(16000, dtype=np.float32), 16000))
    assert reply == {"text": "Where can I build a Thinkery?", "seconds": 0.4, "audio_seconds": 1.0}
    assert len(ears.got) == 1


def test_listen_says_when_it_heard_nothing(voiced):
    url, _, _ = voiced(ears=FakeEars(text=""))
    reply = listen(url, to_wav(np.zeros(1600, dtype=np.float32), 16000))
    assert reply["text"] == "" and reply["say"]["text"] == server.NOT_HEARD
    assert get(url + reply["say"]["url"]) == b"RIFF" + server.NOT_HEARD.encode()
    assert "say" not in listen(url, to_wav(np.zeros(1600, dtype=np.float32), 16000), speak=False)


def test_listen_refuses_other_sites_and_bad_audio(voiced):
    url, _, _ = voiced()
    with pytest.raises(urllib.error.HTTPError) as e:
        listen(url, b"x", content_type="application/x-www-form-urlencoded")
    assert e.value.code == 415  # a cross-site form post cannot send audio/wav
    with pytest.raises(urllib.error.HTTPError) as e:
        listen(url, b"not a wav")
    assert e.value.code == 400


def test_speech_urls_that_do_not_exist(voiced):
    url, state, _ = voiced()
    with pytest.raises(urllib.error.HTTPError) as e:
        get(url + "/api/speech/abcdefgh/0")
    assert e.value.code == 404
    state.narrator.start("cancelled1")
    f = Future()
    f.cancel()
    state.narrator._clips["cancelled1"] = [f]
    with pytest.raises(urllib.error.HTTPError) as e:
        get(url + "/api/speech/cancelled1/0")
    assert e.value.code == 410


def test_status_reports_voice_and_mic(voiced):
    url, _, _ = voiced()
    status = json.loads(get(url + "/api/status"))
    assert status["voice"] == {"problem": "", "ready": True}
    assert status["mic"] == {"problem": "", "ready": False}
    assert b"registerProcessor" in get(url + "/recorder.js")


def test_typed_ask_still_works_with_the_shared_helper(voiced):
    url, _, _ = voiced()
    events = ask(url, "thinkery")
    assert events[0][0] == "entries" and events[-1][0] == "done"
