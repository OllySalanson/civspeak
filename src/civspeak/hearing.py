"""Turn the recorded question into text: Whisper base.en on the CPU, with a hint list of Civ names.

Whisper spells unusual names badly on its own ("M-Bansa", "zigurate"). Its hotwords and prompt
(text it treats as having come just before the recording) are filled with names from the player's
own index, so it has just seen "Mbanza" and "Kongo" before hearing them. Those two slots hold
about 380 tokens between them, roughly 90 names out of the Civilopedia's thousands, so the hint
list favours civilization-specific buildings, civilizations, leaders and wonders: proper names
Whisper cannot guess. Anything it still mishears is caught by the sound-alike title match in
search.py, and Claude is told the question was spoken.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from . import paths

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
# Whisper's decoder holds 448 tokens. faster-whisper gives the hint two slots of up to 223 tokens
# each (the "hotwords" and the prompt); together they keep well clear of the limit, leaving room
# for the question itself.
HINT_TOKENS = 190

# Kinds of page, in the order they are worth hinting, with how many names each takes per round.
# Generic pages (Campus, Harbor, Archer) are ordinary English that Whisper already spells; the
# civilization-specific ones and proper names are what it gets wrong.
HINT_KINDS = [
    ("unique infrastructure", 3),
    ("civilization", 1),
    ("leader", 2),
    ("wonder", 2),
    ("unique unit", 1),
    ("city-state", 1),
    ("great person", 1),
]


def hint_kind(section: str, group: str, unique: bool) -> str:
    if unique and section in ("DISTRICTS", "BUILDINGS", "IMPROVEMENTS"):
        return "unique infrastructure"
    if unique and section == "UNITS":
        return "unique unit"
    if section == "CIVILIZATIONS":
        return {"Civilizations": "civilization", "Leaders": "leader"}.get(group, "")
    return {"WONDERS": "wonder", "CITYSTATES": "city-state", "GREATPEOPLE": "great person"}.get(section, "")


_WORD = re.compile(r"[^\W\d_][^\W\d_'\-]*", re.UNICODE)


def hint_names(pages: Iterable[tuple[str, str, str, bool]], count_tokens: Callable[[str], int], budget: int) -> list[list[str]]:
    """Page names Whisper most needs to see, as lists that each fit in `budget` tokens.

    `pages` is (section id, group, title, unique-to-a-civ) in Civilopedia order; `count_tokens`
    counts Whisper tokens. A name is worth hinting when one of its words takes Whisper more than
    one token to spell. Kinds of page take turns (see HINT_KINDS), so each kind gets a share.
    """
    queues: dict[str, list[str]] = {kind: [] for kind, _ in HINT_KINDS}
    seen = set()
    for section, group, title, unique in pages:
        kind = hint_kind(section, group, unique)
        name = title.split(" (")[0].strip()  # "Cleopatra (Egyptian)" -> "Cleopatra"
        if kind and name.lower() not in seen and any(count_tokens(" " + w) >= 2 for w in _WORD.findall(name)):
            seen.add(name.lower())
            queues[kind].append(name)

    order = []
    while any(queues.values()):
        for kind, take in HINT_KINDS:
            q = queues[kind]
            order.extend(q[:take])
            del q[:take]

    lists: list[list[str]] = [[]]
    used = 0
    for name in order:
        need = count_tokens(" " + name + ",")
        if used + need > budget:
            if len(lists) == 2:
                break
            lists.append([])
            used = 0
        lists[-1].append(name)
        used += need
    return [names for names in lists if names]


def hint_texts(pages, count_tokens: Callable[[str], int], budget: int = HINT_TOKENS) -> tuple[str, str]:
    """(hotwords, prompt) for Whisper: two comma-separated lists of Civ names."""
    lists = hint_names(pages, count_tokens, budget) + [[], []]
    return ", ".join(lists[0]), ", ".join(lists[1]) + ("." if lists[1] else "")


@dataclass
class Heard:
    text: str
    seconds: float
    audio_seconds: float


class Ears:
    """faster-whisper (from the local Hugging Face cache only), loaded on first use or by warm-up and
    kept until it has been idle a while (see idle.py)."""

    def __init__(self, model: str | None = None, threads: int | None = None):
        self.model_name = model or paths.whisper_model()
        self.threads = threads or paths.cpu_threads()
        self._model = None
        self._lock = threading.Lock()
        self._hint_for: object = None
        self._hint = ("", "")
        self.error = ""
        self.last_used = time.monotonic()

    def problem(self) -> str:
        """Why the microphone cannot be used on this PC, or "" if it can."""
        if self.error:
            return self.error
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return "Speech recognition is not installed. Run ./scripts/setup.sh to add it."
        return ""

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        self._acquire()

    def unload(self) -> bool:
        with self._lock:
            loaded, self._model = self._model is not None, None
        return loaded

    def _acquire(self):
        """The loaded model (loading it if needed), taken under the lock so an idle unload cannot race it."""
        with self._lock:
            self.last_used = time.monotonic()
            if self._model is None:
                self._load_locked()
            return self._model

    def _load_locked(self) -> None:
        problem = self.problem()
        if problem:
            raise RuntimeError(problem)
        from faster_whisper import WhisperModel

        started = time.monotonic()
        try:
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type="int8", cpu_threads=self.threads, local_files_only=True
            )
        except Exception as e:  # missing from the cache, or a broken download
            self.error = (
                f"The Whisper {self.model_name} model is not in ~/.cache/huggingface, "
                "so the microphone is off. Run ./scripts/setup.sh to fetch it."
            )
            raise RuntimeError(self.error) from e
        log.info("speech recognition loaded in %.1fs", time.monotonic() - started)

    def hint(self, index) -> tuple[str, str]:
        """The Civ name hint lists for this index (rebuilt when the index is replaced)."""
        model = self._acquire()
        if index is not None and self._hint_for is not index:
            tok = model.hf_tokenizer
            self._hint = hint_texts(index.name_list(), lambda s: len(tok.encode(s, add_special_tokens=False).ids))
            self._hint_for = index
        return self._hint

    def transcribe(self, wav: bytes, index=None) -> Heard:
        """Text of a recording, sent by the page as a mono 16-bit WAV."""
        samples = read_wav(wav)
        hotwords, prompt = self.hint(index)
        model = self._acquire()
        started = time.monotonic()
        segments, info = model.transcribe(
            samples,
            language="en",
            beam_size=5,
            hotwords=hotwords or None,
            initial_prompt=prompt or None,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        took = time.monotonic() - started
        self.last_used = time.monotonic()
        log.info("heard %.1fs of audio in %.1fs", info.duration, took)
        return Heard(clean_transcript(text), round(took, 2), round(info.duration, 2))

    def warm_up(self, index=None) -> None:
        """Load the model and the hint list, and run it once: the first transcription is the slowest."""
        self.hint(index)
        import numpy as np

        quiet = (np.random.default_rng(0).standard_normal(SAMPLE_RATE) * 0.001).astype(np.float32)
        segments, _ = self._acquire().transcribe(quiet, language="en", beam_size=1, without_timestamps=True)
        list(segments)  # transcription runs as the segments are read


class BadAudio(ValueError):
    pass


def read_wav(data: bytes):
    """Mono 16-bit PCM WAV -> float32 samples at 16 kHz (Whisper's rate)."""
    import numpy as np

    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() not in (1, 2):
                raise BadAudio("expected 16-bit PCM WAV")
            rate, channels = w.getframerate(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    except (wave.Error, EOFError) as e:
        raise BadAudio(f"not a WAV file: {e}") from None
    samples = pcm.astype(np.float32) / 32768.0
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    if rate != SAMPLE_RATE and len(samples):
        n = int(len(samples) * SAMPLE_RATE / rate)
        samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)
    return samples


def clean_transcript(text: str) -> str:
    """Whisper sometimes echoes its prompt or adds a stock phrase on silence; drop those."""
    t = text.strip()
    if t.lower().rstrip(".!") in {"", "you", "thank you", "thanks for watching"}:
        return ""
    return t
