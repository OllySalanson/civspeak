"""`Civspeak.exe --selftest`: proves a packaged Civspeak has everything it needs, without a game, a
Claude key, a microphone or a screen. The release build runs it on the build machine before it makes
the installer, so a missing DLL or data file fails the build instead of a player's first question.

Each check is one line of the report: "ok <name>: <detail>" or "FAIL <name>: <why>", written as
soon as it is known. A check gets CHECK_SECONDS; one that takes longer fails, with where it was
stuck. The report goes to standard output and, with --report FILE, to that file too (the packaged
app has no console). The exit code is the number of failed checks; the caller (cli) ends the
process at once, so a stuck check's thread cannot keep it running.

With the voice and speech model files in place (--models FOLDER, or the app's own models folder),
George also says a sentence and Whisper writes it back down. With CIVSPEAK_SELFTEST_ONLINE=1 (the
release build, on a fresh Windows machine with no key anywhere), it also asks the live answer service
a question, as a player's first question would.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from . import __version__, paths, system

CHECK_SECONDS = 120  # a check that takes longer is reported as stuck, and the next one runs
CHECK_SENTENCE = "A Campus gets plus one Science from each adjacent Mountain."


def _web() -> str:
    names = ["index.html", "app.js", "style.css", "favicon.svg", "recorder.js"]
    sizes = [len(resources.files("civspeak").joinpath("web", n).read_bytes()) for n in names]
    return f"{len(names)} page files, {sum(sizes)} bytes"


def _sqlite() -> str:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    return f"SQLite {sqlite3.sqlite_version} with FTS5"


def _claude() -> str:
    import anthropic

    anthropic.Anthropic(api_key="sk-ant-selftest-not-a-key", base_url="https://api.anthropic.com", max_retries=0)
    return f"anthropic {anthropic.__version__}"


def _answers() -> str:
    """With no key of its own, this Civspeak gets an answer from the answer service (a real question)."""
    from . import online
    from .answer import Answerer
    from .search import Hit

    a = Answerer()
    if a.own_key():
        raise RuntimeError(f"a key of its own is set up ({a.keys.where}), so the answer service is not used")
    page = {
        "title": "Campus",
        "section": "Districts",
        "group": "",
        "facts": [{"heading": "Placement", "lines": ["Must be built on flat land."]}],
        "chapters": [],
    }
    started = time.monotonic()
    done = None
    for kind, value in a.stream("Where can I build a Campus?", [Hit(page, 1.0)]):
        if kind == "done":
            done = value
    if done is None or not done.text:
        raise RuntimeError("the answer service gave no answer")
    return f"{online.install_id()} was answered in {time.monotonic() - started:.1f}s: {done.text!r}"


def _voice() -> str:
    import onnxruntime
    from kokoro_onnx.tokenizer import Tokenizer

    phonemes = Tokenizer().phonemize("Campus", "en-gb")  # espeak-ng's library and its data
    if not phonemes.strip():
        raise RuntimeError("espeak-ng gave no phonemes")
    return f"onnxruntime {onnxruntime.__version__}, espeak-ng says Campus as {phonemes.strip()!r}"


def _ears() -> str:
    import av
    import ctranslate2
    import faster_whisper
    from faster_whisper.vad import get_vad_model

    get_vad_model()  # the silence detector's own small model file
    stub = getattr(av, "CIVSPEAK_STUB", False)
    if getattr(sys, "frozen", False) and not stub:
        raise RuntimeError("the real PyAV is bundled (67 MB, GPL codecs); the build should use the stub")
    return f"faster-whisper {faster_whisper.__version__}, ctranslate2 {ctranslate2.__version__}" + (
        ", PyAV left out" if stub else ""
    )


def _sound() -> str:
    if not system.IS_WINDOWS:
        return "skipped: the app's own microphone and speakers are Windows only"
    import sounddevice

    return f"sounddevice {sounddevice.__version__}, {sounddevice.get_portaudio_version()[1]}"


def _window() -> str:
    if not system.IS_WINDOWS:
        return "skipped: the page window is Windows only"
    import webview
    from webview.platforms import winforms

    del webview
    return f"pywebview on .NET, page renderer {winforms.renderer}"


def _child() -> str:
    """Civspeak can start itself as a background process (how the index is rebuilt) and read its output."""
    done = subprocess.run(
        system.civspeak_command("version"), capture_output=True, text=True, timeout=120, **system.background_process_options()
    )
    said = done.stdout.strip()
    if done.returncode != 0 or said != __version__:
        raise RuntimeError(f"exit {done.returncode}, said {said!r}, errors {done.stderr.strip()[-300:]!r}")
    return f"{' '.join(system.civspeak_command('version'))} answered {said}"


def _speak_and_hear(folder: Path) -> Callable[[], str]:
    def check() -> str:
        from . import models
        from .hearing import Ears
        from .speech import Voice

        missing = models.missing(folder)
        if missing:
            raise RuntimeError(f"{len(missing)} model files missing from {folder}")
        os.environ["CIVSPEAK_MODELS_DIR"] = str(folder)
        started = time.monotonic()
        wav = Voice().synthesize(CHECK_SENTENCE)
        spoke = time.monotonic() - started
        heard = Ears().transcribe(wav).text
        if "campus" not in heard.lower():
            raise RuntimeError(f"George said {CHECK_SENTENCE!r} but Whisper heard {heard!r}")
        return f"George spoke in {spoke:.1f}s and Whisper heard {heard!r}"

    return check


def run(report: Path | None = None, models_folder: Path | None = None, deadline: float = CHECK_SECONDS) -> int:
    checks: list[tuple[str, Callable[[], str]]] = [
        ("page", _web),
        ("index", _sqlite),
        ("claude", _claude),
        ("voice", _voice),
        ("ears", _ears),
        ("sound", _sound),
        ("window", _window),
        ("child", _child),
    ]
    folder = models_folder or paths.models_dir()
    from . import models

    if models_folder is not None or not models.missing(folder):
        checks.append(("speak and hear", _speak_and_hear(folder)))
    if os.environ.get("CIVSPEAK_SELFTEST_ONLINE") == "1":
        checks.append(("answers", _answers))
    out = open(report, "w", encoding="utf-8", buffering=1) if report is not None else None  # noqa: SIM115

    def say(line: str) -> None:
        # Each line is written as soon as it is known, so a check that never returns still leaves
        # the report saying which one it was.
        for stream in (out, sys.stdout):
            if stream is not None:
                stream.write(line + "\n")
                stream.flush()

    if system.IS_WINDOWS:
        # A library that fails to load must fail the check, not open a Windows error box nobody will
        # click (SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX).
        import ctypes

        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x8000)
    say(f"Civspeak {__version__} self-test ({'packaged' if getattr(sys, 'frozen', False) else 'from source'})")
    failed = 0
    try:
        for name, check in checks:
            say(f"... {name}")
            ok, detail = _within(check, deadline)
            if ok:
                say(f"ok {name}: {detail}")
            else:
                failed += 1
                say(f"FAIL {name}: {detail}")
        say("all checks passed" if not failed else f"{failed} check(s) failed")
    finally:
        if out is not None:
            out.close()
    return failed


def _within(check: Callable[[], str], deadline: float) -> tuple[bool, str]:
    """Run one check on its own thread: (True, what it found) or (False, why it failed or that it
    took longer than `deadline` seconds, with where it was stuck)."""
    result: list[tuple[bool, str]] = []

    def work() -> None:
        try:
            result.append((True, check()))
        except Exception as e:  # every failure is reported, then the next check runs
            where = "\n".join("    " + line for line in traceback.format_exc().rstrip().splitlines()[-6:])
            result.append((False, f"{type(e).__name__}: {e}\n{where}"))

    thread = threading.Thread(target=work, name="selftest-check", daemon=True)
    thread.start()
    thread.join(deadline)
    if result:
        return result[0]
    frame = sys._current_frames().get(thread.ident)
    stack = "".join(traceback.format_stack(frame)[-8:]) if frame is not None else ""
    return False, f"no answer after {deadline:.0f} seconds; it was stuck here:\n" + "\n".join(
        "    " + line for line in stack.rstrip().splitlines()
    )
