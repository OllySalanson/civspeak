"""Fails if anything git would commit is game content, the built index or a secret."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("repo_guard", ROOT / "tools" / "repo_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.mark.skipif(not (ROOT / ".git").exists() or not shutil.which("git"), reason="not a git checkout")
def test_nothing_staged_or_tracked_is_game_content_or_a_secret():
    files = guard.files_to_commit(ROOT)
    assert files, "expected tracked or staged files"
    problems = guard.check(ROOT, files, guard.index_sentences(guard.default_index()))
    assert problems == []


def test_guard_catches_each_kind_of_leak(tmp_path):
    secret = "sk-ant-" + "x" * 40
    contents = {
        "data/DebugGameplay.sqlite": b"",
        "Text/Some_Text.xml": b"",
        "notes/anthropic-key": b"",
        "src/leak.py": f"KEY = '{secret}'".encode(),
        # Split so this test file itself does not contain a text row.
        "src/rows.py": b'<Row Tag="LOC_SOMETHING">' + b"<Te" + b"xt>words</Text></Row>",
        "docs/copied.md": b"Intro. " + b"This sentence was copied straight out of the player's own Civilopedia pages." + b" End.",
        "src/fine.py": b"print('hello')",
    }
    sentences = {guard._norm("This sentence was copied straight out of the player's own Civilopedia pages.")}
    problems = guard.check(tmp_path, list(contents), sentences, read=lambda p: contents[p])
    flagged = {p.split(":")[0] for p in problems}
    assert flagged == set(contents) - {"src/fine.py"}


def test_gitignore_keeps_game_files_and_secrets_out():
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    probes = [
        "DebugGameplay.sqlite",
        "index.sqlite",
        "Civilopedia_Text.xml",
        "Localisation.sql",
        "anthropic-key",
        "spend.jsonl",
        "mod.modinfo",
        ".venv/bin/python",
    ]
    out = subprocess.run(["git", "check-ignore", "--no-index", *probes], cwd=ROOT, capture_output=True, text=True)
    assert sorted(out.stdout.split()) == sorted(probes)
