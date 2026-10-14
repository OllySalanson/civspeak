"""Each build is a new numbered index file, so a rebuild never has to replace a file the running
server has open (Windows refuses that). Old files go once nothing holds them."""

import os
from pathlib import Path

from civspeak import cli, index, server
from civspeak.answer import Answerer
from civspeak.search import Index
from civspeak.spend import Ledger


def test_each_build_is_a_new_file_and_the_old_one_goes(fake_game, tmp_path):
    folder = tmp_path / "data"
    assert index.current(folder) is None
    first = index.build(fake_game, folder).index
    assert first.name == "index-1.sqlite" and index.current(folder) == first
    second = index.build(fake_game, folder).index
    assert second.name == "index-2.sqlite" and index.current(folder) == second
    assert sorted(p.name for p in folder.iterdir()) == ["index-2.sqlite"]


def test_an_index_still_open_elsewhere_is_kept_until_it_can_go(fake_game, tmp_path, monkeypatch):
    folder = tmp_path / "data"
    first = index.build(fake_game, folder).index
    real_unlink = Path.unlink

    def windows_unlink(self, missing_ok=False):
        if self == first:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", windows_unlink)
    second = index.build(fake_game, folder).index
    assert index.current(folder) == second
    assert first.exists()  # the server still had it open
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert index.remove_old(folder) == []
    assert not first.exists()


def test_the_single_index_of_earlier_versions_is_used_then_replaced(fake_game, tmp_path):
    folder = tmp_path / "data"
    built = index.build(fake_game, folder).index
    legacy = folder / index.LEGACY_NAME
    os.replace(built, legacy)
    assert index.current(folder) == legacy
    assert not index.is_stale(legacy, fake_game)
    fresh = index.build(fake_game, folder).index
    assert index.current(folder) == fresh and not legacy.exists()


def test_cut_off_builds_are_cleared_after_a_while(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    old = folder / f"index-building-1{index.PARTIAL}"
    recent = folder / f"index-building-2{index.PARTIAL}"
    old.write_bytes(b"x")
    recent.write_bytes(b"x")
    os.utime(old, (1, 1))
    index.remove_old(folder)
    assert not old.exists() and recent.exists()  # a build may still be writing the recent one


def test_the_server_switches_to_an_index_built_while_it_runs(built_index, fake_game, tmp_path, monkeypatch):
    out, _ = built_index
    monkeypatch.setattr(server.paths, "discover", lambda: fake_game)
    state = server.State(out.parent, Answerer(Ledger(tmp_path / "spend.jsonl"), tmp_path / "no-key"))
    assert state.index.path == out
    newer = index.build(fake_game, out.parent).index  # `civspeak build` in another window
    state._last_check = 0
    state.check()
    assert state.index.path == newer and not state.building
    assert not out.exists()  # closed by the server, then removed
    assert state.status()["pages"] == 8


def test_the_server_rebuilds_in_a_separate_low_priority_process(built_index, fake_game, tmp_path, monkeypatch):
    out, _ = built_index
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        index.build(fake_game, out.parent)
        return server.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server.system, "background_process_options", lambda: {"creationflags": 0x4000})
    state = server.State(out.parent, Answerer(Ledger(tmp_path / "spend.jsonl"), tmp_path / "no-key"))
    state._rebuild()
    ((cmd, kwargs),) = calls
    assert cmd[-1] == "build" and kwargs["creationflags"] == 0x4000
    assert state.index.path.name == "index-2.sqlite" and state.build_error == ""


def test_the_packaged_app_builds_with_a_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_build", lambda args: seen.append(args.command) or 0)
    assert cli.main(["--build"]) == 0
    assert seen == ["build"]


def test_an_index_path_with_a_quote_opens(fake_game, tmp_path):
    out = index.build(fake_game, tmp_path / "Sid Meier's data").index
    assert Index(out).page_count == 8
