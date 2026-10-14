"""A small log of how long each question took, in ~/.local/share/civspeak/latency.jsonl.

One JSON line per question: when, whether it was spoken, how long listening (speech to text),
searching, Claude's first words, George's first sentence and the whole answer took, and whether
the voice or the speech recognizer had to be loaded first. `civspeak status` summarises it.
Only timings are kept: never the question, the answer or anything else that was said.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path

KEEP = 500  # questions kept; the file is cut back to this many when it reaches twice that


class LatencyLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def record(self, entry: dict) -> None:
        line = json.dumps({"at": round(time.time(), 1), **entry}, sort_keys=True)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._trim()
            except OSError:
                pass  # a full or read-only disk must never break an answer

    def _trim(self) -> None:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if len(lines) >= 2 * KEEP:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines[-KEEP:]) + "\n", encoding="utf-8")
            tmp.replace(self.path)

    def recent(self, n: int = 20) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def first_words_seconds(entry: dict) -> float | None:
    """From the question to George's first word: listening (if spoken) plus the time to the first sentence."""
    voice = entry.get("first_voice_s")
    if voice is None:
        return None
    return round(voice + (entry.get("listen_s") or 0), 2)


def summary(entries: list[dict]) -> str:
    """One line for `civspeak status`, e.g. "first words after 4.1s (median of 12; slowest 7.9s)"."""
    words = [s for s in (first_words_seconds(e) for e in entries) if s is not None]
    texts = [e["first_text_s"] for e in entries if e.get("first_text_s")]
    if words:
        return f"first words after {statistics.median(words):.1f}s (median of {len(words)}; slowest {max(words):.1f}s)"
    if texts:
        return f"first answer text after {statistics.median(texts):.1f}s (median of {len(texts)})"
    return "no questions timed yet"
