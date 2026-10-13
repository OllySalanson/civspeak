"""Rebuild the game's English text table from its XML and SQL text files.

Mirrors how the game's database loader treats LocalizedText files: <Row> and <Replace> write a
row, <Delete> removes matching rows, <Update> changes matching rows. Only English is kept.
"""

from __future__ import annotations

import logging
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger(__name__)

LANGUAGE = "en_US"
# Table names the game maps onto LocalizedText.
TEXT_TABLES = {"LocalizedText", "BaseGameText", "EnglishText", "FrontEndText"}
COLUMNS = ("Language", "Tag", "Text", "Gender", "Plurality")

_SCHEMA = """
CREATE TABLE LocalizedText(
    Language TEXT NOT NULL,
    Tag TEXT NOT NULL,
    Text TEXT,
    Gender TEXT,
    Plurality TEXT,
    PRIMARY KEY (Language, Tag));
"""
# Views so SQL text files written against the other table names still work.
_VIEW = """
CREATE VIEW {name} AS SELECT * FROM LocalizedText;
CREATE TRIGGER {name}_insert INSTEAD OF INSERT ON {name} BEGIN
    INSERT OR REPLACE INTO LocalizedText VALUES
        (COALESCE(NEW.Language, 'en_US'), NEW.Tag, NEW.Text, NEW.Gender, NEW.Plurality);
END;
"""


class TextDB:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(_SCHEMA)
        for name in sorted(TEXT_TABLES - {"LocalizedText"}):
            self.db.executescript(_VIEW.format(name=name))
        self.loaded: list[Path] = []
        self.failed: list[tuple[Path, str]] = []

    # -- loading ---------------------------------------------------------------------------

    def load(self, path: Path) -> bool:
        suffix = path.suffix.lower()
        try:
            if suffix == ".xml":
                self._load_xml(path)
            elif suffix == ".sql":
                self._load_sql(path)
            else:
                return False
        except (ET.ParseError, sqlite3.Error, OSError, UnicodeDecodeError) as e:
            self.failed.append((path, str(e)))
            log.warning("could not load %s: %s", path, e)
            return False
        self.loaded.append(path)
        return True

    def _load_sql(self, path: Path) -> None:
        sql = path.read_text(encoding="utf-8-sig", errors="replace")
        self.db.execute("SAVEPOINT textfile")
        try:
            for statement in _split_sql(sql):
                self.db.execute(statement)
        except sqlite3.Error:
            self.db.execute("ROLLBACK TO textfile")
            self.db.execute("RELEASE textfile")
            raise
        self.db.execute("RELEASE textfile")
        self.db.execute("DELETE FROM LocalizedText WHERE Language <> ?", (LANGUAGE,))

    def _load_xml(self, path: Path) -> None:
        root = ET.parse(path).getroot()
        ops: list[tuple] = []
        for table in root:
            if table.tag not in TEXT_TABLES:
                continue
            for action in table:
                kind = action.tag
                if kind in ("Row", "Replace"):
                    row = _columns(action)
                    if row.get("Tag") and row.get("Language", LANGUAGE) == LANGUAGE:
                        ops.append(("put", row))
                elif kind == "Delete":
                    ops.append(("delete", _columns(action)))
                elif kind == "Update":
                    where = action.find("Where")
                    set_ = action.find("Set")
                    if where is not None and set_ is not None:
                        ops.append(("update", _columns(where), _columns(set_)))
        # Apply the whole file at once so a bad file leaves no half-written state.
        self.db.execute("SAVEPOINT textfile")
        try:
            for op in ops:
                if op[0] == "put":
                    row = op[1]
                    self.db.execute(
                        "INSERT OR REPLACE INTO LocalizedText VALUES (?,?,?,?,?)",
                        (LANGUAGE, row["Tag"], row.get("Text"), row.get("Gender"), row.get("Plurality")),
                    )
                elif op[0] == "delete":
                    where, args = _where(op[1])
                    self.db.execute(f"DELETE FROM LocalizedText WHERE {where}", args)
                else:
                    where, wargs = _where(op[1])
                    sets = {k: v for k, v in op[2].items() if k in COLUMNS}
                    if not sets:
                        continue
                    assign = ", ".join(f"{k} = ?" for k in sets)
                    self.db.execute(
                        f"UPDATE OR REPLACE LocalizedText SET {assign} WHERE {where}",
                        list(sets.values()) + wargs,
                    )
        except sqlite3.Error:
            self.db.execute("ROLLBACK TO textfile")
            self.db.execute("RELEASE textfile")
            raise
        self.db.execute("RELEASE textfile")

    # -- reading ---------------------------------------------------------------------------

    def tags(self) -> dict[str, str]:
        rows = self.db.execute("SELECT Tag, Text FROM LocalizedText WHERE Language = ? AND Text IS NOT NULL", (LANGUAGE,))
        return dict(rows)

    def plurality(self) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT Tag, Plurality FROM LocalizedText WHERE Language = ? AND Plurality IS NOT NULL",
            (LANGUAGE,),
        )
        return dict(rows)


def _columns(el: ET.Element) -> dict[str, str]:
    """Columns may be attributes or child elements (<Text>...</Text>)."""
    cols = {k: v for k, v in el.attrib.items() if k in COLUMNS}
    for child in el:
        if child.tag in COLUMNS:
            cols[child.tag] = child.text or ""
    return cols


def _where(cols: dict[str, str]) -> tuple[str, list[str]]:
    cols = {k: v for k, v in cols.items() if k in COLUMNS}
    if not cols:
        return "1", []
    return " AND ".join(f"{k} = ?" for k in cols), list(cols.values())


def _split_sql(sql: str) -> list[str]:
    """Split a SQL script into single statements, respecting quotes and comments."""
    statements: list[str] = []
    start = 0
    pos = sql.find(";")
    while pos != -1:
        chunk = sql[start : pos + 1]
        if sqlite3.complete_statement(chunk):
            if chunk.strip(" \t\r\n;"):
                statements.append(chunk)
            start = pos + 1
        pos = sql.find(";", pos + 1)
    return statements
