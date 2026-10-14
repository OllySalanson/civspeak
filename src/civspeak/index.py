"""Build the searchable Civilopedia index from the player's own game files.

Recipe: the gameplay rules from the game's cache (DebugGameplay.sqlite, written whenever a game
loads, so it holds exactly the rule set of the last game including DLC and mods) plus the
English text rebuilt by replaying the text files listed in Modding.log for that same game.
The index is written to civspeak's data folder (see paths.data_dir) and never into the repository.

Each build is a new numbered file (index-1.sqlite, index-2.sqlite, ...) and the highest number is
the current one. A running server keeps reading its own file while a new one is written, and
Windows refuses to replace or delete a file another process has open, so a build never touches the
current file: old ones are removed once nothing has them open any more.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import modlog, mods, paths
from .loc import Localizer
from .pedia import Page, Pedia
from .phonetic import phrase_key
from .search import normalize

log = logging.getLogger(__name__)

SCHEMA_VERSION = "2"
PREFIX, SUFFIX = "index-", ".sqlite"
PARTIAL = ".sqlite.part"
LEGACY_NAME = "index.sqlite"  # the single index file of earlier versions
ABANDONED_SECONDS = 3600  # a build still unfinished after this long was cut off (builds time out after 10 minutes)


@dataclass
class BuildReport:
    pages: int
    text_files: int
    tags: int
    unresolved: list[str]
    seconds: float
    index: Path


def source_stamp(game: paths.GamePaths) -> dict[str, float]:
    return {str(p): p.stat().st_mtime for p in game.sources() if p.exists()}


def is_stale(index: Path | None, game: paths.GamePaths) -> bool:
    if index is None or not index.exists():
        return True
    try:
        meta = read_meta(index)
    except sqlite3.Error:
        return True
    if meta.get("schema") != SCHEMA_VERSION:
        return True
    return json.loads(meta.get("sources", "{}")) != source_stamp(game)


def read_meta(index: Path) -> dict[str, str]:
    db = sqlite3.connect(paths.sqlite_uri(index), uri=True)
    try:
        return dict(db.execute("SELECT key, value FROM meta"))
    finally:
        db.close()


def load_text(game: paths.GamePaths, session: modlog.GameSession):
    from .textdb import TextDB

    text = TextDB()
    base_files = sorted(game.base_text_dir.glob("*.xml")) if game.base_text_dir.is_dir() else []
    for f in base_files:
        text.load(f)
    plan = mods.plan_text(session, mods.scan_modinfos(game.mod_roots))
    for item in plan.files:
        text.load(item.path)
    return text, plan, len(base_files)


def build(game: paths.GamePaths | None = None, folder: Path | None = None) -> BuildReport:
    """Build a new index into `folder` (civspeak's data folder by default); it becomes the current one."""
    started = time.monotonic()
    game = game or paths.discover()
    folder = folder or paths.data_dir()
    for needed in (game.gameplay_db, game.modding_log):
        if not needed.exists():
            raise paths.SetupError(f"{needed} is missing. Start or load a game in Civ VI once, then try again.")

    stamp = source_stamp(game)
    session = modlog.parse(game.modding_log.read_text(encoding="utf-8", errors="replace"))
    text, plan, base_count = load_text(game, session)
    tags = text.tags()
    loc = Localizer(tags)

    # Work on a private copy: the game may rewrite its cache at any moment.
    with tempfile.TemporaryDirectory(prefix="civspeak-") as tmp:
        db_copy = Path(tmp) / "gameplay.sqlite"
        shutil.copyfile(game.gameplay_db, db_copy)
        gameplay = sqlite3.connect(paths.sqlite_uri(db_copy), uri=True)
        try:
            pages = Pedia(gameplay, loc).pages()
        finally:
            gameplay.close()

    meta = {
        "schema": SCHEMA_VERSION,
        "built_at": str(time.time()),
        "sources": json.dumps(stamp, sort_keys=True),
        "game_loaded_at": str(game.gameplay_db.stat().st_mtime),
        "mods": json.dumps(sorted(session.mods.values())),
        "text_files": str(base_count + len(plan.files)),
        "unresolved": json.dumps(plan.unresolved),
    }
    out = write_index(pages, folder, meta)
    return BuildReport(
        pages=len(pages),
        text_files=base_count + len(plan.files),
        tags=len(tags),
        unresolved=plan.unresolved,
        seconds=time.monotonic() - started,
        index=out,
    )


def write_index(pages: list[Page], folder: Path, meta: dict[str, str]) -> Path:
    """Write a new numbered index file and return it; it is complete before it gets its final name."""
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"index-building-{os.getpid()}{PARTIAL}"
    if tmp.exists():
        tmp.unlink()
    db = sqlite3.connect(tmp)
    try:
        db.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE pages(
                id INTEGER PRIMARY KEY,
                uid TEXT UNIQUE,
                section_id TEXT, section TEXT, page_id TEXT, layout TEXT,
                title TEXT, grp TEXT, terms TEXT,
                facts TEXT, chapters TEXT, text TEXT);
            CREATE TABLE names(page INTEGER, name TEXT, norm TEXT, sound TEXT, kind TEXT);
            CREATE VIRTUAL TABLE pages_fts USING fts5(
                title, terms, text, content='pages', content_rowid='id',
                tokenize="porter unicode61 remove_diacritics 2");
            """
        )
        db.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
        for i, p in enumerate(pages, start=1):
            facts = [{"heading": g.heading, "lines": g.lines} for g in p.facts]
            chapters = [{"title": c.title, "paragraphs": c.paragraphs} for c in p.chapters]
            db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    i,
                    p.uid,
                    p.section_id,
                    p.section,
                    p.page_id,
                    p.layout,
                    p.title,
                    p.group,
                    "\n".join(p.terms),
                    json.dumps(facts),
                    json.dumps(chapters),
                    p.full_text(),
                ),
            )
            for name, kind in [(p.title, "title")] + [(t, "term") for t in p.terms]:
                db.execute("INSERT INTO names VALUES (?,?,?,?,?)", (i, name, normalize(name), phrase_key(name), kind))
        db.execute("INSERT INTO pages_fts(pages_fts) VALUES ('rebuild')")
        db.commit()
    finally:
        db.close()
    versions = _versions(folder)
    out = folder / f"{PREFIX}{versions[-1][0] + 1 if versions else 1}{SUFFIX}"
    os.replace(tmp, out)  # a new name, so this works on Windows even while the old index is open
    remove_old(folder)
    return out


def _versions(folder: Path) -> list[tuple[int, Path]]:
    """The finished index files in folder, oldest first."""
    found = []
    for p in folder.glob(f"{PREFIX}*{SUFFIX}"):
        number = p.name[len(PREFIX) : -len(SUFFIX)]
        if number.isdigit():
            found.append((int(number), p))
    return sorted(found)


def current(folder: Path | None = None) -> Path | None:
    """The newest finished index in folder (civspeak's data folder by default), or None if there is none yet."""
    folder = folder or paths.data_dir()
    versions = _versions(folder)
    if versions:
        return versions[-1][1]
    legacy = folder / LEGACY_NAME
    return legacy if legacy.exists() else None


def remove_old(folder: Path) -> list[Path]:
    """Delete every index but the current one, and builds that were cut off. An old index another process
    still has open stays (Windows refuses to delete it) and goes next time. Returns what is left over."""
    versions = _versions(folder)
    old = [p for _, p in versions[:-1]]
    if versions:
        old.append(folder / LEGACY_NAME)
    cutoff = time.time() - ABANDONED_SECONDS
    old += [p for p in folder.glob(f"index-building-*{PARTIAL}") if _mtime(p) < cutoff]
    left = []
    for p in old:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            left.append(p)
    return left


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()
