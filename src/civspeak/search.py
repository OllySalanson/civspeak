"""Find the Civilopedia entries a question is about.

Three signals, combined:
- title match: page names (and the Civilopedia's own search terms) found in the question,
  exactly, fuzzily ("zigurat" -> Ziggurat) or by sound ("hanza" -> Hansa);
- full-text search (SQLite FTS5, BM25) over titles, search terms and page text;
- a small down-weight for Historic Moments, whose names reuse district and wonder names.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .phonetic import ascii_fold, phrase_key

STOPWORDS = set(
    """a an the of to in on at for from by with and or but is are was were be been being do does did
    doing have has had what whats what's which who whom whose where when why how can could should would
    will shall may might must i me my we our you your he she it its they them their this that these those
    there here about into onto over under up down out than then so if not no yes any some all each
    tell explain mean means meaning get got make made give gives use used using need needs want work works
    thing things one ones much many more most need build built building place placed put civ civilopedia
    please just like does do""".split()
)
# Words that are part of many titles but carry little meaning on their own.
WEAK = {"great", "district", "age", "era", "unit", "the", "of"}
SECTION_WEIGHT = {"MOMENTS": 0.7}
# Better Civilopedia's AI pages are titled like eras ("Medieval Era") but only list what the AI favours.
LAYOUT_WEIGHT = {"TimeStrategy": 0.4}
PLACEMENT_WORDS = {"where", "build", "built", "place", "placed", "placement", "put", "adjacent", "tile", "next"}

_WORD = re.compile(r"[a-z0-9]+")


def singular(tok: str) -> str:
    if len(tok) <= 3 or tok.endswith(("ss", "us", "is")):
        return tok
    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    if tok.endswith(("ches", "shes", "xes", "sses", "zes")):
        return tok[:-2]
    if tok.endswith("s"):
        return tok[:-1]
    return tok


# Questions are matched after singular(), so "does" arrives as "doe": stop those forms too.
STOPWORDS |= {singular(w) for w in STOPWORDS}


def tokens(text: str) -> list[str]:
    return [singular(t) for t in _WORD.findall(ascii_fold(text).lower().replace("'", ""))]


def normalize(text: str) -> str:
    return " ".join(tokens(text))


@dataclass
class Hit:
    page: dict
    score: float
    why: list[str] = field(default_factory=list)


@dataclass
class _Name:
    page: int
    norm: str
    sound: str
    compact: str
    kind: str
    toks: tuple[str, ...]


class Index:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.meta = dict((r["key"], r["value"]) for r in self.db.execute("SELECT key, value FROM meta"))
        self.sections = dict(self.db.execute("SELECT id, section_id FROM pages").fetchall())
        self.layouts = dict(self.db.execute("SELECT id, layout FROM pages").fetchall())
        self.names: list[_Name] = []
        self.by_norm: dict[str, list[_Name]] = defaultdict(list)
        self.by_sound: dict[str, list[_Name]] = defaultdict(list)
        self.by_token: dict[str, list[_Name]] = defaultdict(list)
        self.by_len: dict[int, list[str]] = defaultdict(list)
        self.by_first: dict[str, list[str]] = defaultdict(list)
        for r in self.db.execute("SELECT page, norm, sound, kind FROM names"):
            toks = tuple(r["norm"].split())
            if not toks:
                continue
            n = _Name(r["page"], r["norm"], r["sound"], r["sound"].replace(" ", ""), r["kind"], toks)
            self.names.append(n)
            self.by_norm[n.norm].append(n)
            self.by_sound[n.sound].append(n)
            if n.compact != n.sound:
                self.by_sound[n.compact].append(n)
            for t in set(toks):
                self.by_token[t].append(n)
        for norm in self.by_norm:
            self.by_len[len(norm.split())].append(norm)
            self.by_first[norm[:4]].append(norm)
        self.placeable = {r[0] for r in self.db.execute("""SELECT id FROM pages WHERE facts LIKE '%"heading": "Placement"%'""")}
        self.sound_keys = sorted(k for k in self.by_sound if len(k) >= 3 and " " not in k)

    def close(self) -> None:
        self.db.close()

    @property
    def page_count(self) -> int:
        return len(self.sections)

    # -- pages -----------------------------------------------------------------------------

    def page(self, page_id: int) -> dict:
        r = self.db.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        return {
            "id": r["id"],
            "uid": r["uid"],
            "title": r["title"],
            "section": r["section"],
            "section_id": r["section_id"],
            "group": r["grp"],
            "layout": r["layout"],
            "terms": [t for t in (r["terms"] or "").split("\n") if t],
            "facts": json.loads(r["facts"]),
            "chapters": json.loads(r["chapters"]),
        }

    def titles(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT title FROM pages ORDER BY title")]

    def name_list(self) -> list[tuple[str, str, str, bool]]:
        """(section id, group, title, unique to a civilization) of every page, in Civilopedia order."""
        rows = self.db.execute("SELECT section_id, grp, title, facts FROM pages ORDER BY id")
        return [(r[0], r[1] or "", r[2], any(g["heading"] == "Unique To" for g in json.loads(r[3]))) for r in rows]

    # -- search ----------------------------------------------------------------------------

    def search(self, question: str, k: int = 6) -> list[Hit]:
        qtoks = tokens(question)
        content = [t for t in qtoks if t not in STOPWORDS]
        scores: dict[int, float] = defaultdict(float)
        why: dict[int, list[str]] = defaultdict(list)

        def bump(page: int, score: float, reason: str) -> None:
            if score > scores[page]:
                scores[page] = score
                why[page] = [reason]

        grams: dict[str, int] = {}
        for n in range(1, min(6, len(qtoks)) + 1):
            for i in range(len(qtoks) - n + 1):
                gram = qtoks[i : i + n]
                if all(t in STOPWORDS for t in gram):
                    continue
                grams[" ".join(gram)] = n

        for gram, n in grams.items():
            exact = self.by_norm.get(gram, [])
            for name in exact:
                weak = all(t in WEAK or t in STOPWORDS for t in name.toks)
                bump(name.page, (0.6 if weak else 1.0) + 0.05 * n - (0.05 if name.kind == "term" else 0), f"names '{gram}'")
            if exact or all(t in STOPWORDS or t in WEAK or len(t) < 3 for t in gram.split()):
                continue  # an exact name needs no fuzzy or sound-alike guesses
            if n == 1 and len(gram) >= 4:
                # A word that starts a name: "koto" -> "Kotoku-in".
                for norm in self.by_first.get(gram[:4], []):
                    first = norm.split()[0]
                    if first != gram and first.startswith(gram) and len(gram) / len(first) >= 0.5:
                        for name in self.by_norm[norm]:
                            bump(name.page, 0.55, f"starts like '{gram}'")
            gsound = phrase_key(gram)
            for skey in {gsound, gsound.replace(" ", "")}:
                for name in self.by_sound.get(skey, []):
                    if len(skey) >= 3:
                        bump(name.page, 0.82 + 0.03 * n, f"sounds like '{gram}'")
            for norm in difflib.get_close_matches(gram, self.by_len.get(n, []), n=4, cutoff=0.8):
                ratio = difflib.SequenceMatcher(None, gram, norm).ratio()
                for name in self.by_norm[norm]:
                    bump(name.page, 0.9 * ratio + 0.03 * n, f"close to '{gram}'")

        if max(scores.values(), default=0) < 0.8:
            # Nothing named outright: the speech recognizer may have misheard a name, so accept a
            # near match on its sound ("embancer" -> Mbanza, "amanita" -> Amanitore).
            for gram, n in grams.items():
                if n > 2 or any(t in STOPWORDS or t in WEAK for t in gram.split()):
                    continue
                compact = phrase_key(gram).replace(" ", "")
                if len(compact) < 4:
                    continue
                for skey in difflib.get_close_matches(compact, self.sound_keys, n=3, cutoff=0.75):
                    ratio = difflib.SequenceMatcher(None, compact, skey).ratio()
                    for name in self.by_sound[skey]:
                        bump(name.page, 0.7 * ratio + 0.03 * n, f"sounds close to '{gram}'")

        # Multi-word names whose meaningful words all appear somewhere in the question.
        qset = set(content)
        for t in qset:
            for name in self.by_token.get(t, []):
                strong = [x for x in name.toks if x not in STOPWORDS]
                if len(strong) < 2:
                    continue
                matched = [x for x in strong if x in qset]
                coverage = len(matched) / len(strong)
                if coverage == 1:
                    bump(name.page, 0.78, "all name words")
                elif coverage >= 0.5 and any(x not in WEAK for x in matched):
                    bump(name.page, 0.45 * coverage, "some name words")

        for rank, (page, _bm25) in enumerate(self._fulltext(content)):
            fts = 0.35 * (1 - rank / 40)
            scores[page] += fts
            if not why[page]:
                why[page] = ["text match"]

        placement = bool(PLACEMENT_WORDS & set(qtoks))
        ranked = []
        for page, score in scores.items():
            score *= SECTION_WEIGHT.get(self.sections.get(page, ""), 1.0)
            score *= LAYOUT_WEIGHT.get(self.layouts.get(page, ""), 1.0)
            if placement and page in self.placeable:
                score += 0.05  # "where can I build X": X is something with placement rules
            ranked.append((score, page))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        hits = [Hit(self.page(p), round(s, 3), why[p]) for s, p in ranked[:k] if s > 0.05]
        if hits and placement:
            self._add_section_concept(hits)
        return hits

    def _add_section_concept(self, hits: list[Hit]) -> None:
        """Placement questions: add the Concepts page that explains the top entry's kind of thing
        (e.g. the "Districts" concept page holds the general district placement rules)."""
        top = hits[0].page
        if top["section_id"] == "CONCEPTS":
            return
        wanted = {normalize(top["section"]), normalize(top["group"] or "")} - {""}
        for name_norm in wanted:
            for name in self.by_norm.get(name_norm, []):
                if self.sections.get(name.page) == "CONCEPTS" and all(h.page["id"] != name.page for h in hits):
                    hits.append(Hit(self.page(name.page), 0.0, ["general placement rules"]))
                    return

    def _fulltext(self, content: list[str]) -> list[tuple[int, float]]:
        words = [t for t in dict.fromkeys(content) if len(t) > 1]
        if not words:
            return []
        query = " OR ".join(f'"{w}"' for w in words)
        try:
            rows = self.db.execute(
                "SELECT rowid, bm25(pages_fts, 10.0, 6.0, 1.0) AS rank FROM pages_fts "
                "WHERE pages_fts MATCH ? ORDER BY rank LIMIT 40",
                (query,),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [(r["rowid"], r["rank"]) for r in rows]


# -- presenting entries -----------------------------------------------------------------------


def entry_text(page: dict, budget: int = 3000) -> str:
    """Plain text of a page for the answer prompt: facts first, then chapters, within a budget."""
    head = f"{page['title']} ({page['section']}{' - ' + page['group'] if page['group'] else ''})"
    parts = [head]
    used = len(head)
    blocks = []
    for g in page["facts"]:
        body = "; ".join(g["lines"])
        blocks.append(f"{g['heading']}: {body}" if g["heading"] else body)
    for c in page["chapters"]:
        text = "\n".join(c["paragraphs"])
        blocks.append(f"{c['title']}:\n{text}" if c["title"] else text)
    # Facts first, then chapters; a long page (every district's adjacency, say) is cut to fit.
    for block in blocks:
        room = budget - used
        if room <= 200:
            break
        if len(block) > room:
            block = block[:room].rsplit(" ", 1)[0] + " ..."
        parts.append(block)
        used += len(block)
    return "\n".join(parts)
