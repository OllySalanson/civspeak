"""Unload George's voice and the speech recognizer after a while unused, so an idle Civspeak holds
about 1 GB less memory while the game runs. The next question loads them again; the page and the
Windows helper wake them as soon as the talk key or the microphone is pressed, so the reload mostly
happens while the player is still speaking.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Protocol

log = logging.getLogger(__name__)

CHECK_SECONDS = 30


class Model(Protocol):
    loaded: bool
    last_used: float

    def unload(self) -> bool: ...


def trim_memory() -> None:
    """Hand memory freed by an unloaded model back to the system (glibc keeps it otherwise)."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


class IdleUnloader:
    """Checks every CHECK_SECONDS and unloads each model unused for `after` seconds (0 = never)."""

    def __init__(self, models: list[Model], after: float, clock=time.monotonic):
        self.models = models
        self.after = after
        self.clock = clock
        self._stop = threading.Event()

    def start(self) -> None:
        if self.after > 0:
            threading.Thread(target=self._run, name="idle-unload", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(CHECK_SECONDS):
            self.check()

    def check(self) -> list[Model]:
        """Unload what has been idle long enough; returns what was unloaded."""
        now = self.clock()
        done = [m for m in self.models if m.loaded and now - m.last_used >= self.after and m.unload()]
        if done:
            trim_memory()
            log.info("unloaded %d idle model(s) after %.0f minutes unused", len(done), self.after / 60)
        return done
