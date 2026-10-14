"""The Windows app's own microphone and speakers, through sounddevice (PortAudio comes inside its wheel).

Recording and playing in this process rather than in the page means ` held in the game works with the
page's window closed, and WebView2 never needs the microphone or permission to play sound.

- Recording holds one spoken question and decides when it is over, by the page's own rules
  (web/app.js): a tap listens until you stop talking, a hold until you let go.
- Microphone opens the default input for one question at a time, at 16 kHz (Whisper's rate) when the
  device allows it, and closes again: Windows shows the microphone in use only while you ask.
- Speakers plays George's sentences one after another on its own thread; a new answer, a new
  question or Esc cuts him off at once.

sounddevice and numpy are imported on first use, so this module imports without them (and in WSL,
where the page records and plays instead).
"""

from __future__ import annotations

import io
import logging
import queue
import threading
import wave
from collections.abc import Callable
from concurrent.futures import Future

log = logging.getLogger(__name__)

RATE = 16000  # Whisper's rate; asked of the microphone first
BLOCK_SECONDS = 0.1  # the microphone hands over this much at a time
# The page's rules for when a question is over (web/app.js).
SPEECH_LEVEL = 0.012  # RMS above this counts as talking, after the browser's automatic gain...
# ...which a raw microphone does not have: a quiet one in a quiet room hears talking as little as this
# (RMS), so talking is anything ROOM_MARGIN times the room's own level, between the two.
QUIET_SPEECH_LEVEL = 0.002
ROOM_MARGIN = 10
FULL_LEVEL = 0.08 / SPEECH_LEVEL  # the microphone button shows full at this many times the talking level
END_SILENCE = 1.2  # this much quiet after talking ends a tapped question
NOTHING_SAID = 6.0  # a tapped question gives up if nobody talks for this long
MAX_SECONDS = 15.0
WRITE_SECONDS = 0.05  # George is written to the speakers in pieces this long, so Esc stops him at once
LINGER_SECONDS = 0.5  # the speakers stay open this long after a sentence, for the next one

NO_MODULE = "Microphone support is not installed. Reinstall Civspeak to add it."
NO_MICROPHONE = "No microphone was found. Plug one in, then ask again."
MUTED = (
    "Windows is not letting Civspeak use the microphone. Turn on Settings > Privacy & security > Microphone > "
    "Let desktop apps access your microphone, then try again."
)


class Recording:
    """One spoken question as it comes in from the microphone, and whether it is over yet."""

    def __init__(self, mode: str, rate: int = RATE):
        self.mode = mode  # "hold" (until the key is let go) or "toggle" (until the talking stops)
        self.rate = rate
        self.blocks: list = []
        self.samples = 0
        self.heard = False  # anything above SPEECH_LEVEL
        self.silent = True  # nothing but exact zeros: what Windows hands over when it blocks the microphone
        self.level = 0.0  # loudness of the last block, 0 to 1, for the page's microphone button
        self.room = float("inf")  # the quietest block so far: the room with nobody talking
        self._last_voice = 0

    @property
    def seconds(self) -> float:
        return self.samples / self.rate

    @property
    def speech_level(self) -> float:
        """RMS above which this microphone is hearing someone talk."""
        return min(SPEECH_LEVEL, max(QUIET_SPEECH_LEVEL, self.room * ROOM_MARGIN))

    def add(self, block) -> bool:
        """Add a block of float32 samples; True when the question is over."""
        import numpy as np

        self.blocks.append(block)
        self.samples += len(block)
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if len(block) else 0.0
        speech = self.speech_level
        self.level = min(1.0, rms / (speech * FULL_LEVEL))
        self.silent = self.silent and not np.any(block)
        if rms > speech:
            self.heard = True
            self._last_voice = self.samples
        if len(block):
            self.room = min(self.room, rms)
        return self.over()

    def over(self) -> bool:
        if self.seconds >= MAX_SECONDS:
            return True
        if self.mode != "toggle":
            return False
        if self.heard:
            return (self.samples - self._last_voice) / self.rate >= END_SILENCE
        return self.seconds >= NOTHING_SAID

    def wav(self) -> bytes:
        import numpy as np

        from .speech import to_wav

        return to_wav(np.concatenate(self.blocks) if self.blocks else np.zeros(0, np.float32), self.rate)


def read_clip(wav: bytes):
    """A WAV clip of George's (mono 16-bit) as (int16 samples, rate)."""
    import numpy as np

    with wave.open(io.BytesIO(wav), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"), w.getframerate()


def resample(samples, rate: int, to: int):
    """Linear resampling, for a device that will not take the clip's own rate."""
    import numpy as np

    if rate == to or not len(samples):
        return samples
    n = int(len(samples) * to / rate)
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(samples.dtype)


class Sound:
    """sounddevice, loaded on first use. PortAudio lists the microphones and speakers once, when it
    starts, so a device plugged in later is only found by listing them again: done when a stream will
    not open, or no microphone is found, and no other stream is open."""

    def __init__(self, sd=None):
        self._sd = sd
        self._lock = threading.Lock()
        self._open = 0

    def module(self):
        if self._sd is None:
            import sounddevice

            self._sd = sounddevice
        return self._sd

    def problem(self) -> str:
        """Why there is no microphone to record from, or ""."""
        try:
            sd = self.module()
        except (ImportError, OSError):  # OSError: PortAudio itself is missing
            return NO_MODULE
        for attempt in range(2):
            try:
                sd.query_devices(kind="input")
                return ""
            except (sd.PortAudioError, ValueError):
                if attempt or not self._refresh():
                    return NO_MICROPHONE
        return NO_MICROPHONE

    def _refresh(self) -> bool:
        """List the devices again, if no stream is open (PortAudio restarts). True when it did."""
        with self._lock:
            if self._open:
                return False
            sd = self.module()
            sd._terminate()
            sd._initialize()
            return True

    def open(self, make: Callable):
        """A started stream from make(sounddevice), listing the devices again once if it fails."""
        sd = self.module()
        for attempt in range(2):
            try:
                with self._lock:
                    stream = make(sd)
                    stream.start()
                    self._open += 1
                return stream
            except sd.PortAudioError:
                if attempt or not self._refresh():
                    raise
        raise AssertionError("unreachable")

    def close(self, stream, drain: bool = True) -> None:
        """Stop a stream from open(): let what it holds play out (drain), or cut it off."""
        try:
            stream.stop() if drain else stream.abort()
            stream.close()
        except Exception:
            log.exception("could not close a sound stream")
        finally:
            with self._lock:
                self._open -= 1


class Microphone:
    """The default microphone, open while one question is asked."""

    def __init__(self, sound: Sound):
        self.sound = sound
        self.stream = None

    def open(self, on_block: Callable) -> int:
        """Start recording; on_block(samples) gets each block of mono float32 samples (on PortAudio's
        thread). Returns the sample rate. Raises sounddevice.PortAudioError when it cannot."""
        self.close()

        def callback(indata, frames, time_info, status):
            on_block(indata[:, 0].copy())

        def make(rate):
            def opener(sd):
                rate_ = rate or int(sd.query_devices(kind="input")["default_samplerate"])
                return sd.InputStream(
                    samplerate=rate_, channels=1, dtype="float32", blocksize=int(rate_ * BLOCK_SECONDS), callback=callback
                )

            return opener

        sd = self.sound.module()
        try:
            self.stream = self.sound.open(make(RATE))
        except sd.PortAudioError:  # the device will not record at 16 kHz itself: its own rate, resampled later
            self.stream = self.sound.open(make(None))
        return int(self.stream.samplerate)

    def close(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            self.sound.close(stream, drain=False)


class Speakers:
    """Plays George's sentences in order on its own thread. Each answer gets a new turn from begin();
    clips from an older turn are dropped, and the one playing stops within WRITE_SECONDS."""

    def __init__(self, sound: Sound, on_speaking: Callable[[bool], None] = lambda on: None):
        self.sound = sound
        self.on_speaking = on_speaking
        self.turn = 0
        self.speaking = False
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def begin(self) -> int:
        """A new answer: stop the old one. Returns the turn to play() this answer's clips with."""
        with self._lock:
            self.turn += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="speakers", daemon=True)
                self._thread.start()
            self._queue.put(None)  # wakes the thread, which notices the new turn
            return self.turn

    def stop(self) -> None:
        self.begin()

    def play(self, clip: Future, turn: int) -> None:
        """Play a sentence (a Future of its WAV) after the ones before it, if `turn` is still current."""
        self._queue.put((turn, clip))

    def _set_speaking(self, on: bool) -> None:
        if on != self.speaking:
            self.speaking = on
            self.on_speaking(on)

    def _run(self) -> None:
        stream = None
        while True:
            try:
                item = self._queue.get(timeout=LINGER_SECONDS if stream is not None else None)
            except queue.Empty:  # nothing more to say: let the last sentence play out and close
                self.sound.close(stream)
                stream = None
                self._set_speaking(False)
                continue
            if item is None or item[0] != self.turn:
                if stream is not None:
                    self.sound.close(stream, drain=False)
                    stream = None
                if self._queue.empty():
                    self._set_speaking(False)
                continue
            turn, clip = item
            try:
                samples, rate = read_clip(clip.result(timeout=60))
            except Exception:  # cancelled by a newer answer, or George could not voice it: skip it
                continue
            if turn != self.turn:  # stopped while George was still voicing it
                continue
            try:
                stream = self._write(stream, samples, rate, turn)
            except Exception:
                log.exception("could not play George's voice")
                if stream is not None:
                    self.sound.close(stream, drain=False)
                stream = None

    def _write(self, stream, samples, rate: int, turn: int):
        """Write one sentence, a piece at a time; returns the stream to keep using (None once cut off)."""
        if stream is not None and int(stream.samplerate) != rate:
            self.sound.close(stream)
            stream = None
        if stream is None:
            stream = self._open(rate)
        samples = resample(samples, rate, int(stream.samplerate))
        piece = max(1, int(stream.samplerate * WRITE_SECONDS))
        self._set_speaking(True)
        for start in range(0, len(samples), piece):
            if turn != self.turn:  # Esc, a new question or a new answer
                self.sound.close(stream, drain=False)
                return None
            stream.write(samples[start : start + piece])
        return stream

    def _open(self, rate: int):
        sd = self.sound.module()

        def make(rate_):
            return lambda sd: sd.OutputStream(samplerate=rate_, channels=1, dtype="int16")

        try:
            return self.sound.open(make(rate))
        except sd.PortAudioError:  # the speakers will not play Kokoro's rate: theirs, resampled
            return self.sound.open(make(int(sd.query_devices(kind="output")["default_samplerate"])))
