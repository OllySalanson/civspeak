"""Build the searchable Civilopedia index from the player's own game files.

Recipe: the gameplay rules from the game's cache (DebugGameplay.sqlite, written whenever a game
loads, so it holds exactly the rule set of the last game including DLC and mods) plus the
English text rebuilt by replaying the text files listed in Modding.log for that same game.
The index is written to ~/.local/share/civspeak and never into the repository.
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

SCHEMA_VERSION = "1"


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


def is_stale(index: Path, game: paths.GamePaths) -> bool:
    if not index.exists():
        return True
    try:
        meta = read_meta(index)
    except sqlite3.Error:
        return True
    if meta.get("schema") != SCHEMA_VERSION:
        return True
    return json.loads(meta.get("sources", "{}")) != source_stamp(game)


def read_meta(index: Path) -> dict[str, str]:
    db = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
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


def build(game: paths.GamePaths | None = None, out: Path | None = None) -> BuildReport:
    started = time.monotonic()
    game = game or paths.discover()
    out = out or paths.index_file()
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
        gameplay = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
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
    write_index(pages, out, meta)
    return BuildReport(
        pages=len(pages),
        text_files=base_count + len(plan.files),
        tags=len(tags),
        unresolved=plan.unresolved,
        seconds=time.monotonic() - started,
        index=out,
    )


def write_index(pages: list[Page], out: Path, meta: dict[str, str]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + f".tmp{os.getpid()}")
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
    os.replace(tmp, out)
