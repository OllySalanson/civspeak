"""The Windows app asks and answers by itself: ` held in the game (or the page's microphone button)
records from this PC's microphone, Whisper writes the question down, Claude answers and George reads
the answer out of its speakers, whether or not the page's window is open.

Every open page is told what is happening on its event stream (/api/talk), so it shows the same thing
it would have shown had it recorded the question itself:

  mic       {"state": "idle"|"listening"|"working", "mode": "hold"|"toggle"}
  level     {"level": 0..1} the microphone's loudness, ten times a second while listening
  question  {"text": "..."} what was heard, just before the answer's own events
  entries, delta, say, done, problem   as on /api/ask (problem also for the microphone)
  speaking  {"on": true|false} George started or stopped talking

The rules are the page's (web/app.js): hold to talk and let go when done, or tap to talk until you
stop. Each question (spoken here or typed in the page) is a new turn; an answer from an older turn
goes quiet but runs to its end, so its real cost is settled in the spend ledger.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import audio
from .hearing import BadAudio
from .server import NOT_HEARD, Answering, Speaker

log = logging.getLogger(__name__)

HOLD_SECONDS = 0.35  # a press longer than this is hold-to-talk; a shorter one a tap
STALL_SECONDS = 2.0  # no sound from the microphone for this long: it went away
NOTHING_HEARD = "I didn't hear anything. Ask again, a little closer to the microphone."
NOT_CAUGHT = "Sorry, I didn't catch that. Try again a little closer to the microphone."
NO_START = "Could not start the microphone. Is one plugged in?"
NO_ANSWER = "Something went wrong answering that. Try again."


class Conversation:
    def __init__(self, state, talk, sound: audio.Sound | None = None, clock=time.monotonic):
        self.state = state  # server.State
        self.talk = talk  # server.Talk: every open page
        self.sound = sound or audio.Sound()
        self.mic = audio.Microphone(self.sound)
        self.speakers = audio.Speakers(self.sound, lambda on: self.talk.broadcast("speaking", {"on": on}))
        self.clock = clock
        self.lock = threading.RLock()
        self.mic_state = "idle"
        self.mode = "toggle"
        self.pressed_at = 0.0
        self.recording: audio.Recording | None = None
        self.blocks: queue.SimpleQueue = queue.SimpleQueue()  # the recording's sound, not yet taken
        self.arrived = threading.Event()  # set when the microphone hands over more
        self.turn = 0
        self.speaker: Speaker | None = None  # George reading the current turn's answer
        self.speak = True  # the page's Voice on/off (it says on loading and when it changes)

    def problem(self) -> str:
        """Why questions cannot be asked out loud here, or ""."""
        return self.sound.problem() or self.state.ears.problem()

    # -- what the player does ------------------------------------------------------------------

    def press(self, key: str) -> bool:
        """` or the microphone button went "down" or "up", or Esc ("cancel"). False when there is no
        microphone to listen with (the page is told why)."""
        if key == "down":
            problem = self.problem()
            if problem:
                self._problem(problem)
                return False
            self.state.wake()  # George and Whisper load again while the player is still asking
        with self.lock:
            if key == "down":
                self.pressed_at = self.clock()
                if self.mic_state == "listening" and self.mode == "toggle":
                    self._finish()
                elif self.mic_state == "idle":
                    self._start("hold")
            elif key == "up":
                if self.mic_state != "listening" or self.mode != "hold":
                    return True
                if self.clock() - self.pressed_at < HOLD_SECONDS:
                    self.mode = self.recording.mode = "toggle"  # a tap: keep listening until the talking stops
                    self._show_mic()
                else:
                    self._finish()
            elif key == "cancel" and self.mic_state == "listening":
                self.recording = None
                self.mic.close()
                self._show_mic("idle")
        return True

    def hush(self) -> None:
        """Esc, Stop, or Voice off: George stops talking."""
        with self.lock:
            if self.speaker is not None:
                self.speaker.cancel()
                self.speaker = None
            self.speakers.stop()

    def typed(self, event, speak: bool) -> Speaker | None:
        """A question typed in the page: an answer still going here goes quiet, and George reads the new
        one out here (when the page wants it spoken). Returns its speaker."""
        with self.lock:
            self._new_turn()
            self.speaker = self._speaker(event) if speak else None
            return self.speaker

    # -- listening -----------------------------------------------------------------------------

    def _new_turn(self) -> None:
        self.turn += 1
        self.hush()

    def _start(self, mode: str) -> None:
        self._new_turn()
        self.mode = mode
        self.blocks, self.arrived = blocks, arrived = queue.SimpleQueue(), threading.Event()

        def on_block(block) -> None:  # on PortAudio's thread: only hand it over
            blocks.put(block)
            arrived.set()

        try:
            rate = self.mic.open(on_block)
        except Exception:
            log.exception("could not start the microphone")
            self._problem(NO_START)
            return
        self.recording = audio.Recording(mode, rate)
        self._show_mic("listening")
        threading.Thread(target=self._listen, args=(self.recording,), name="listen", daemon=True).start()

    def _listen(self, recording: audio.Recording) -> None:
        """Takes the microphone's sound until the question is over, let go of, or cancelled. Sound is
        only ever taken under the lock, so none is lost when the key is let go meanwhile."""
        while True:
            arrived = self.arrived.wait(STALL_SECONDS)
            with self.lock:
                if self.recording is not recording:
                    return
                if not arrived:
                    log.warning("the microphone stopped sending sound")
                    self._finish()
                    return
                self.arrived.clear()
                over = False
                while not self.blocks.empty() and not over:
                    over = recording.add(self.blocks.get())
                self.talk.broadcast("level", {"level": round(recording.level, 3)})
                if over:
                    self._finish()
                    return

    def _finish(self) -> None:
        """The question is over: Whisper writes it down and Claude answers it, on their own thread."""
        recording, self.recording = self.recording, None
        self.mic.close()
        if recording is None:
            return
        while not self.blocks.empty():  # the last moment before the key was let go
            recording.add(self.blocks.get())
        if not recording.samples:
            self._show_mic("idle")
            self._problem(NOTHING_HEARD)
            return
        if recording.silent:
            self._show_mic("idle")
            self._problem(audio.MUTED)
            return
        if recording.mode == "toggle" and not recording.heard:
            self._show_mic("idle")
            self._problem(NOTHING_HEARD)
            self._say(NOT_HEARD)
            return
        self._show_mic("working")
        threading.Thread(target=self._answer, args=(self.turn, recording.wav()), name="answer", daemon=True).start()

    # -- answering -----------------------------------------------------------------------------

    def _answer(self, turn: int, wav: bytes) -> None:
        try:
            heard = self.state.hear(wav)
        except (BadAudio, RuntimeError) as e:  # RuntimeError: Whisper could not be loaded
            with self.lock:
                if self.mic_state == "working":
                    self._show_mic("idle")
                if turn == self.turn:
                    self._problem(str(e))
            return
        with self.lock:
            if self.mic_state == "working":
                self._show_mic("idle")
            if turn != self.turn:  # a question was typed meanwhile
                return
            if not heard.text:
                self._problem(NOT_CAUGHT)
                self._say(NOT_HEARD)
                return
            self.talk.broadcast("question", {"text": heard.text})
            event = self._event_for(turn)
            self.speaker = self._speaker(event) if self.speak else None
            speaker = self.speaker
        try:
            Answering(self.state, event).run(heard.text, speaker, spoken=True)
        except Exception:
            log.exception("could not answer a spoken question")
            event("problem", {"kind": "api", "message": NO_ANSWER})

    def _event_for(self, turn: int):
        """Tells the pages about this turn's answer, until a newer turn starts."""

        def event(name: str, data) -> None:
            with self.lock:
                if turn == self.turn:
                    self.talk.broadcast(name, data)

        return event

    def _speaker(self, event) -> Speaker | None:
        if self.state.voice.problem():
            return None
        return Speaker(self.state.narrator, event, self.speakers)

    def _say(self, sentence: str) -> None:
        if not self.speak:
            return
        self.speaker = self._speaker(lambda name, data: None)
        if self.speaker is not None:
            self.speaker.say(sentence)

    # -- telling the pages ---------------------------------------------------------------------

    def _show_mic(self, state: str | None = None) -> None:
        if state is not None:
            self.mic_state = state
        self.talk.broadcast("mic", {"state": self.mic_state, "mode": self.mode})

    def _problem(self, message: str) -> None:
        self.talk.broadcast("problem", {"kind": "mic", "message": message})
