"""`Civspeak.exe --selftest`: proves a packaged Civspeak has everything it needs, without a game, a
Claude key, a microphone or a screen. The release build runs it on the build machine before it makes
the installer, so a missing DLL or data file fails the build instead of a player's first question.

Each check is one line of the report: "ok <name>: <detail>" or "FAIL <name>: <why>". The report goes
to standard output and, with --report FILE, to that file too (the packaged app has no console).
The exit code is the number of failed checks.

With the voice and speech model files in place (--models FOLDER, or the app's own models folder),
George also says a sentence and Whisper writes it back down.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from . import __version__, paths, system

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


def run(report: Path | None = None, models_folder: Path | None = None) -> int:
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
    lines = [f"Civspeak {__version__} self-test ({'packaged' if getattr(sys, 'frozen', False) else 'from source'})"]
    failed = 0
    for name, check in checks:
        try:
            lines.append(f"ok {name}: {check()}")
        except Exception as e:  # every failure is reported, then the next check runs
            failed += 1
            lines.append(f"FAIL {name}: {type(e).__name__}: {e}")
            lines += ["    " + line for line in traceback.format_exc().rstrip().splitlines()[-6:]]
    lines.append("all checks passed" if not failed else f"{failed} check(s) failed")
    text = "\n".join(lines) + "\n"
    if report is not None:
        report.write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        sys.stdout.write(text)
    return failed
