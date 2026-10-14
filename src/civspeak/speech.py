"""George reads the answer out: Kokoro (voice bm_george at 0.92) on the CPU, one sentence at a time.

The answer streams from Claude as text; SentenceSplitter cuts it into sentences as soon as each one
is complete, so the first sentence is being voiced while Claude is still writing the rest.
Kokoro is loaded once and kept; all synthesis runs on a single worker thread so two answers never
fight over the CPU.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
import wave
from concurrent.futures import Future, ThreadPoolExecutor

from . import paths

log = logging.getLogger(__name__)

VOICE = "bm_george"
SPEED = 0.92
LANG = "en-gb"
SENTENCE_PAUSE = 0.3  # seconds of quiet after each sentence
CLAUSE_PAUSE = 0.12

# Words ending in a full stop that do not end a sentence.
_ABBREVIATIONS = {"e.g", "i.e", "etc", "vs", "approx", "st", "mt", "dr", "mr", "mrs", "ms", "no"}
_END = re.compile(r"[.!?]+[\"')\]]*\s+")
_CLAUSE = re.compile(r"[,;:]\s+|\s+-\s+")
# A long first sentence is spoken from its first comma on, so George starts sooner.
FIRST_CLAUSE = 35


class SentenceSplitter:
    """Feed streamed text in; get whole sentences out as soon as the next one has begun.

    The first piece may be just the opening clause of a long sentence ("A Campus can go on any
    tile within 3 tiles of your City Center,"): it is the one George waits for.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self.pieces = 0

    def feed(self, text: str) -> list[str]:
        self.buffer += text
        out = []
        start = 0
        for m in _END.finditer(self.buffer):
            if m.end() >= len(self.buffer):
                break  # wait for the next word: "e.g. the" must not split
            before = self.buffer[start : m.start()].rsplit(None, 1)
            word = before[-1].lower().rstrip(".") if before else ""
            if word in _ABBREVIATIONS or (self.buffer[m.end()].islower() and self.buffer[m.start()] == "."):
                continue
            sentence = self.buffer[start : m.end()].strip()
            if sentence:
                out.append(sentence)
            start = m.end()
        self.buffer = self.buffer[start:]
        if not out and not self.pieces:
            for m in _CLAUSE.finditer(self.buffer):
                if m.start() >= FIRST_CLAUSE and m.end() < len(self.buffer):
                    out.append(self.buffer[: m.end()].strip())
                    self.buffer = self.buffer[m.end() :]
                    break
        self.pieces += len(out)
        return out

    def flush(self) -> list[str]:
        rest, self.buffer = self.buffer.strip(), ""
        self.pieces += bool(rest)
        return [rest] if rest else []


def speech_text(sentence: str) -> str:
    """What George should actually say: no markup, and symbols said as words."""
    s = re.sub(r"\[ICON_\w+\]", " ", sentence)  # stray icon codes such as [ICON_Science]
    s = re.sub(r"[*_#`]+", "", s)
    s = re.sub(r"\+\s?(?=\d)", "plus ", s)
    s = re.sub(r"(?<=\d)\s?%", " percent", s)
    s = s.replace("&", " and ")
    return re.sub(r"\s+", " ", s).strip()


def to_wav(samples, rate: int) -> bytes:
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _errors_only(record: logging.LogRecord) -> bool:
    # espeak warns about every clause it splits ("words count mismatch"); it is harmless.
    # (phonemizer resets its logger's level on each use, so this has to be a filter.)
    return record.levelno >= logging.ERROR


class Voice:
    """Kokoro, loaded on first use (or by warm-up) and kept until it has been idle a while (see idle.py)."""

    def __init__(self, voice: str = VOICE, speed: float = SPEED, threads: int | None = None):
        # 4 threads measured: real-time factor ~0.55 (2 threads: ~1, too slow to keep up with speech).
        self.voice, self.speed, self.threads = voice, speed, threads or paths.cpu_threads()
        self._kokoro = None
        self._lock = threading.Lock()
        self.error = ""
        self.last_used = time.monotonic()

    def problem(self) -> str:
        """Why George cannot speak on this PC, or "" if he can."""
        if self.error:
            return self.error
        try:
            import kokoro_onnx  # noqa: F401
            import onnxruntime  # noqa: F401
        except ImportError:
            return "The voice is not installed. Run ./scripts/setup.sh to add it."
        model, voices = paths.kokoro_files()
        if not model.exists() or not voices.exists():
            return f"The Kokoro voice files were not found in {model.parent} (set CIVSPEAK_KOKORO_DIR)."
        return ""

    @property
    def loaded(self) -> bool:
        return self._kokoro is not None

    def load(self) -> None:
        self._acquire()

    def unload(self) -> bool:
        with self._lock:
            loaded, self._kokoro = self._kokoro is not None, None
        return loaded

    def _acquire(self):
        """The loaded Kokoro (loading it if needed), taken under the lock so an idle unload cannot race it."""
        with self._lock:
            self.last_used = time.monotonic()
            if self._kokoro is None:
                self._load_locked()
            return self._kokoro

    def _load_locked(self) -> None:
        problem = self.problem()
        if problem:
            raise RuntimeError(problem)
        import onnxruntime as rt
        from kokoro_onnx import Kokoro

        logging.getLogger("phonemizer").addFilter(_errors_only)
        started = time.monotonic()
        model, voices = paths.kokoro_files()
        options = rt.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        # onnxruntime's threads otherwise spin for a while after every sentence: measured a third
        # less CPU per sentence without it, at the same speed, which leaves those cores to the game.
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        try:
            session = rt.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
            self._kokoro = Kokoro.from_session(session, str(voices))
        except Exception as e:  # a broken model file must not take the server down
            self.error = f"The Kokoro voice could not be loaded: {e}"
            raise RuntimeError(self.error) from e
        log.info("voice loaded in %.1fs", time.monotonic() - started)

    def synthesize(self, sentence: str) -> bytes:
        """One sentence as a WAV file."""
        kokoro = self._acquire()
        text = speech_text(sentence)
        samples, rate = kokoro.create(text, voice=self.voice, speed=self.speed, lang=LANG)
        self.last_used = time.monotonic()
        # Kokoro trims the silence off each clip; put back the pause that belongs after it.
        import numpy as np

        pause = CLAUSE_PAUSE if sentence.rstrip()[-1:] in ",;:-" else SENTENCE_PAUSE
        return to_wav(np.concatenate([samples, np.zeros(int(rate * pause), dtype=samples.dtype)]), rate)


class Narrator:
    """Voices sentences in order on one worker thread. A new answer cancels what is still queued."""

    def __init__(self, voice: Voice, keep: int = 4):
        self.voice = voice
        self.keep = keep
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice")
        self._lock = threading.Lock()
        self._clips: dict[str, list[Future]] = {}

    def start(self, answer_id: str) -> None:
        with self._lock:
            for clips in self._clips.values():
                for f in clips:
                    f.cancel()
            self._clips[answer_id] = []
            while len(self._clips) > self.keep:
                self._clips.pop(next(iter(self._clips)))

    def say(self, answer_id: str, sentence: str) -> int:
        """Queue a sentence; returns its number within the answer."""
        with self._lock:
            clips = self._clips.setdefault(answer_id, [])
            clips.append(self._pool.submit(self._speak, sentence))
            return len(clips) - 1

    def cancel(self, answer_id: str) -> None:
        with self._lock:
            for f in self._clips.get(answer_id, []):
                f.cancel()

    def clip(self, answer_id: str, n: int) -> Future | None:
        with self._lock:
            clips = self._clips.get(answer_id, [])
            return clips[n] if 0 <= n < len(clips) else None

    def _speak(self, sentence: str) -> bytes:
        started = time.monotonic()
        wav = self.voice.synthesize(sentence)
        log.info("voiced %d characters in %.1fs", len(sentence), time.monotonic() - started)
        return wav

    def warm_up(self) -> Future:
        # The first synthesis after loading is slow (onnxruntime sets itself up); do it now.
        return self._pool.submit(self.voice.synthesize, "Ready.")
