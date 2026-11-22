"""The packaged Windows app: the self-test it runs on the build machine, its own command-line forms,
PyAV's stand-in, and that the installer script and the app agree (single-instance lock, start at
login). The build itself runs on Windows in .github/workflows/build.yml."""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

from civspeak import cli, keys, selftest, winhelper

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


def test_the_self_test_passes_from_source_and_writes_its_report(tmp_path, capsys):
    report = tmp_path / "selftest.txt"
    failed = selftest.run(report)
    text = report.read_text()
    assert failed == 0, text
    assert text == capsys.readouterr().out
    lines = text.splitlines()
    assert lines[0].startswith("Civspeak ") and lines[-1] == "all checks passed"
    names = [line.split(":")[0] for line in lines[1:-1]]
    assert names == ["ok page", "ok index", "ok claude", "ok voice", "ok ears", "ok sound", "ok window", "ok child"]


def test_a_failing_check_is_reported_and_counted(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("espeak-ng is missing")

    monkeypatch.setattr(selftest, "_voice", broken)
    report = tmp_path / "r.txt"
    assert selftest.run(report) == 1
    text = report.read_text()
    assert "FAIL voice: RuntimeError: espeak-ng is missing" in text and text.endswith("1 check(s) failed\n")


def test_speaking_and_hearing_is_checked_when_the_models_are_there(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(selftest, "_speak_and_hear", lambda folder: lambda: seen.append(folder) or "heard it")
    assert selftest.run(tmp_path / "r.txt", models_folder=tmp_path) == 0
    assert seen == [tmp_path] and "ok speak and hear: heard it" in (tmp_path / "r.txt").read_text()


@pytest.mark.parametrize(("argv", "command"), [(["--build"], "build"), (["--version"], "version"), (["--selftest"], "selftest")])
def test_the_packaged_app_takes_its_own_commands(argv, command, monkeypatch):
    ran = []
    monkeypatch.setattr(cli, f"cmd_{command}", lambda args: ran.append(command) or 0)
    assert cli.main(argv) == 0 and ran == [command]


def test_version_prints_the_version(capsys):
    from civspeak import __version__

    assert cli.main(["version"]) == 0 and capsys.readouterr().out == f"{__version__}\n"


def test_pyav_stand_in_loads_but_refuses_to_decode():
    spec = importlib.util.spec_from_file_location("av_stub", PACKAGING / "stubs" / "av" / "__init__.py")
    av = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(av)
    assert av.CIVSPEAK_STUB is True and not hasattr(av, "__wrapped__")
    with pytest.raises(ImportError, match="PyAV is not part of the packaged Civspeak"):
        av.open("question.wav")


def test_the_build_installs_pinned_packages_without_pyav():
    pins = [
        line for line in (PACKAGING / "requirements-windows.txt").read_text().splitlines() if line and not line.startswith("#")
    ]
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][A-Za-z0-9.]*", p) for p in pins), pins
    names = {p.split("==")[0].lower().replace("_", "-") for p in pins}
    assert "av" not in names
    assert {"anthropic", "kokoro-onnx", "faster-whisper", "pywebview", "sounddevice", "pyinstaller"} <= names


def test_the_installer_and_the_app_agree():
    iss = (PACKAGING / "civspeak.iss").read_text()
    assert f"AppMutex={winhelper.MUTEX_NAME}" in iss  # Setup waits for a running Civspeak to close
    assert f'ValueName: "{winhelper.NAME}"' in iss and winhelper.RUN_KEY in iss  # the tray menu's own Run value
    assert "PrivilegesRequired=lowest" in iss and r"DefaultDirName={localappdata}\Programs\Civspeak" in iss
    assert f'"/delete:""{keys.CREDENTIAL}"""' in iss  # "remove everything" forgets the key too
    # The Run value is written the way the app writes and compares it (subprocess.list2cmdline):
    # quoted only when the path has a space.
    assert "if Pos(' ', Result) > 0 then" in iss
    for path in (r"C:\Users\Ann\AppData\Local\Programs\Civspeak\Civspeak.exe", r"C:\Users\Ann Lee\Civspeak.exe"):
        assert subprocess.list2cmdline([path]) == (f'"{path}"' if " " in path else path)


def test_the_workflow_runs_the_self_test_and_only_drafts_releases():
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text()
    assert workflow.count('"--selftest"') == 2  # the built folder, then the installed app
    assert "gh release create" in workflow and "--draft" in workflow
    assert 'python-version: "3.12"' in workflow
