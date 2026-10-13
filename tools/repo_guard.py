#!/usr/bin/env python3
"""Refuse to commit Firaxis or mod content, game files, the built index or the API key.

Checks every file git would commit (tracked plus staged, using the staged version):
- no game or index file types (.sqlite, .xml, .sql, .modinfo, .blp, ...), no key files, no voice
  models (.onnx, .bin) and no recordings;
- nothing that looks like an Anthropic API key;
- no game-style text rows (<Row ...><Text>) outside the made-up test fixture;
- if the Civilopedia index has been built on this PC, no sentence from it.

Run directly (the pre-commit hook does) or through tests/test_repo_guard.py.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

FORBIDDEN_SUFFIXES = {
    ".sqlite",
    ".sqlite3",
    ".db",
    ".xml",
    ".sql",
    ".modinfo",
    ".blp",
    ".dds",
    ".log",
    ".wav",
    ".jsonl",
    ".key",
    ".pem",
    ".dep",
    ".artdef",
    ".xlp",
    # Voice models and recordings: Kokoro, Whisper, and any test audio.
    ".onnx",
    ".bin",
    ".mp3",
    ".ogg",
    ".webm",
    ".flac",
}
FORBIDDEN_NAMES = {"anthropic-key", ".env", "spend.jsonl", "index.sqlite"}
# The only file allowed to hold game-style text rows: the invented mini game for tests.
FIXTURE_FILES = {"tests/conftest.py"}
MAX_BYTES = 1_000_000

API_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
TEXT_ROW = re.compile(r"<(Row|Replace)\s+Tag=\"LOC_[A-Z0-9_]+\"[^>]*>\s*<Text>", re.IGNORECASE)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
MIN_SENTENCE = 60


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def files_to_commit(root: Path) -> list[str]:
    out = git(root, "ls-files", "--cached", "-z").decode()
    return [p for p in out.split("\0") if p]


def staged_content(root: Path, path: str) -> bytes:
    try:
        return git(root, "show", f":{path}")
    except subprocess.CalledProcessError:
        return (root / path).read_bytes() if (root / path).is_file() else b""


def _norm(sentence: str) -> str:
    return " ".join(sentence.lower().split())


def index_sentences(index: Path) -> set[str]:
    """Sentences of the player's built Civilopedia (only on a PC where it was built)."""
    if not index.is_file():
        return set()
    db = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    try:
        out = set()
        for (text,) in db.execute("SELECT text FROM pages"):
            for s in SENTENCE_SPLIT.split(text or ""):
                s = _norm(s)
                if len(s) >= MIN_SENTENCE:
                    out.add(s)
        return out
    except sqlite3.Error:
        return set()
    finally:
        db.close()


def default_index() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(os.environ.get("CIVSPEAK_DATA_DIR") or os.path.join(base, "civspeak")) / "index.sqlite"


def check(root: Path, paths: list[str], sentences: set[str], read=None) -> list[str]:
    read = read or (lambda p: staged_content(root, p))
    problems = []
    for path in paths:
        name = Path(path).name
        suffix = Path(path).suffix.lower()
        if name in FORBIDDEN_NAMES or suffix in FORBIDDEN_SUFFIXES:
            problems.append(f"{path}: game, index or secret file type must not be committed")
            continue
        data = read(path)
        if len(data) > MAX_BYTES:
            problems.append(f"{path}: larger than {MAX_BYTES} bytes")
            continue
        text = data.decode("utf-8", errors="ignore")
        if API_KEY.search(text):
            problems.append(f"{path}: contains something that looks like an Anthropic API key")
        if path not in FIXTURE_FILES and TEXT_ROW.search(text):
            problems.append(f"{path}: contains game-style text rows (only tests/conftest.py may, with invented text)")
        if sentences:
            for s in SENTENCE_SPLIT.split(text):
                if _norm(s) in sentences:
                    problems.append(f"{path}: contains Civilopedia text from your game: {s.strip()[:60]}...")
                    break
    return problems


def main() -> int:
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    problems = check(root, files_to_commit(root), index_sentences(default_index()))
    for p in problems:
        print(f"repo guard: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
