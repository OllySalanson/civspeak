"""Build Civilopedia pages from the game's gameplay database, the way the in-game Civilopedia does.

Pages come from the Civilopedia tables (static pages plus CivilopediaPageQueries); chapter text
is found with the same key patterns as CivilopediaSupport.lua; the stats box for the main page
types is a port of the game's CivilopediaPage_*.lua layouts (Gathering Storm versions).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from .loc import Localizer, clean

log = logging.getLogger(__name__)


@dataclass
class FactGroup:
    heading: str
    lines: list[str] = field(default_factory=list)


@dataclass
class Chapter:
    title: str
    paragraphs: list[str]


@dataclass
class Page:
    section_id: str
    section: str
    page_id: str
    layout: str
    title: str
    group: str = ""
    facts: list[FactGroup] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    sort_index: int = 0

    @property
    def uid(self) -> str:
        return f"{self.section_id}/{self.page_id}"

    def facts_text(self) -> str:
        out = []
        for g in self.facts:
            body = "; ".join(g.lines)
            out.append(f"{g.heading}: {body}" if g.heading else body)
        return "\n".join(out)

    def body_text(self) -> str:
        parts = []
        for c in self.chapters:
            text = "\n".join(c.paragraphs)
            parts.append(f"{c.title}\n{text}" if c.title else text)
        return "\n\n".join(parts)

    def full_text(self) -> str:
        head = f"{self.title} ({self.section}{' - ' + self.group if self.group else ''})"
        return "\n\n".join(p for p in (head, self.facts_text(), self.body_text()) if p)


class _Box:
    """One stats box. Lines before the first header go under the box title."""

    def __init__(self, page: Page, loc: Localizer, title_key: str):
        self.page = page
        self.loc = loc
        self.current = FactGroup(self._text(title_key))
        page.facts.append(self.current)

    def _text(self, key, *args) -> str:
        return clean(self.loc.lookup(key, *args))

    def header(self, key, *args) -> None:
        self.current = FactGroup(self._text(key, *args))
        self.page.facts.append(self.current)

    def line(self, key, *args) -> None:
        text = self._text(key, *args).strip("• ").strip()
        if text:
            self.current.lines.append(text)

    def lines(self, header_key, items) -> None:
        items = [i for i in items if i]
        if items:
            self.header(header_key)
            for item in items:
                self.line(item)


class Pedia:
    def __init__(self, db: sqlite3.Connection, loc: Localizer):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.loc = loc
        self._cache: dict[tuple[str, str], dict] = {}
        self._used_traits: set[str] | None = None

    # -- database helpers ------------------------------------------------------------------

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        try:
            return self.db.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []

    def by(self, table: str, col: str) -> dict:
        key = (table, col)
        if key not in self._cache:
            self._cache[key] = {r[col]: r for r in self.q(f"SELECT * FROM {table}")}
        return self._cache[key]

    def get(self, table: str, col: str, value) -> sqlite3.Row | None:
        if value is None:
            return None
        return self.by(table, col).get(value)

    def name(self, table: str, col: str, value, name_col: str = "Name") -> str:
        row = self.get(table, col, value)
        if row is None:
            return ""
        return self.text(_col(row, name_col))

    def text(self, key, *args) -> str:
        return clean(self.loc.lookup(key, *args))

    def param(self, name: str, default: float = 0) -> float:
        rows = self.q("SELECT Value FROM GlobalParameters WHERE Name = ?", name)
        try:
            return float(rows[0]["Value"]) if rows else default
        except (TypeError, ValueError):
            return default

    def modifier_text(self, modifier_id, context: str = "Summary") -> str:
        """A modifier's own description, filled with its arguments (GameEffectsText.lua's GetModifierText)."""
        rows = self.q("SELECT Text FROM ModifierStrings WHERE ModifierId = ? AND Context = ?", modifier_id, context)
        if not rows or not rows[0]["Text"]:
            return ""
        named = {
            a["Name"]: _number_or_text(a["Value"])
            for a in self.q("SELECT * FROM ModifierArguments WHERE ModifierId = ?", modifier_id)
        }
        return clean(self.loc.lookup(rows[0]["Text"], named=named))

    def modifier_args(self, link_table: str, owner_col: str, owner: str, effect: str, arg_name: str) -> list[str]:
        """Values of one argument of an owner's modifiers that have a given effect (e.g. the buildings a belief adds)."""
        mod_col = "ModifierID" if link_table == "BeliefModifiers" else "ModifierId"
        rows = self.q(
            f"SELECT a.Value FROM {link_table} l JOIN Modifiers m ON m.ModifierId = l.{mod_col} "
            "JOIN DynamicModifiers d ON d.ModifierType = m.ModifierType "
            "JOIN ModifierArguments a ON a.ModifierId = m.ModifierId "
            f"WHERE l.{owner_col} = ? AND d.EffectType = ? AND a.Name = ?",
            owner,
            effect,
            arg_name,
        )
        return [r["Value"] for r in rows]

    def yield_row(self, yield_type: str):
        return self.get("Yields", "YieldType", yield_type)

    def type_name(self, type_id: str) -> str:
        """The name the player sees for a game type such as YIELD_SCIENCE or DISTRICT_CAMPUS."""
        for table, col in _NAMED_TYPES:
            name = self.name(table, col, type_id)
            if name:
                return name
        words = type_id.split("_")
        if len(words) > 1 and words[0] in ("PSEUDOYIELD", "YIELD", "TECH", "CIVIC", "BUILDING", "DISTRICT", "UNIT"):
            words = words[1:]
        return " ".join(w.capitalize() for w in words)

    # -- pages -----------------------------------------------------------------------------

    def sections(self) -> dict[str, str]:
        excluded = {r["SectionId"] for r in self.q("SELECT SectionId FROM CivilopediaSectionExcludes")}
        out = {}
        for r in self.q("SELECT SectionId, Name FROM CivilopediaSections ORDER BY SortIndex"):
            if r["SectionId"] not in excluded:
                out[r["SectionId"]] = self.text(r["Name"])
        return out

    def _raw_pages(self) -> list[dict]:
        rows: list[dict] = []
        for r in self.q("SELECT * FROM CivilopediaPages"):
            rows.append(dict(r))
        for q in self.q("SELECT * FROM CivilopediaPageQueries ORDER BY SortIndex, RowId"):
            for r in self.q(q["SQL"]):
                keys = r.keys()

                def col(name, r=r, keys=keys):
                    return r[name] if name and name in keys else None

                rows.append(
                    {
                        "SectionId": q["SectionId"],
                        "PageId": col(q["PageIdColumn"]),
                        "PageGroupId": col(q["PageGroupIdColumn"]),
                        "PageLayoutId": col(q["PageLayoutIdColumn"]),
                        "Name": col(q["NameColumn"]),
                        "TextKeyPrefix": col(q["TextKeyPrefixColumn"]),
                        "SortIndex": col(q["SortIndexColumn"]) or 0,
                    }
                )
        return rows

    def _group_names(self) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        for r in self.q("SELECT SectionId, PageGroupId, Name FROM CivilopediaPageGroups"):
            out[(r["SectionId"], r["PageGroupId"])] = self.text(r["Name"])
        for q in self.q("SELECT * FROM CivilopediaPageGroupQueries"):
            for r in self.q(q["SQL"]):
                keys = r.keys()
                gid = r[q["PageGroupIdColumn"]] if q["PageGroupIdColumn"] in keys else None
                name = r[q["NameColumn"]] if q["NameColumn"] in keys else None
                if gid and name:
                    out[(q["SectionId"], gid)] = self.text(name)
        return out

    def pages(self) -> list[Page]:
        sections = self.sections()
        excluded = {(r["SectionId"], r["PageId"]) for r in self.q("SELECT * FROM CivilopediaPageExcludes")}
        groups = self._group_names()
        terms = self._search_terms()
        layouts = {r["PageLayoutId"]: r["ScriptTemplate"] for r in self.q("SELECT * FROM CivilopediaPageLayouts")}

        pages: list[Page] = []
        seen: set[tuple[str, str]] = set()
        for raw in self._raw_pages():
            section_id, page_id = raw["SectionId"], raw["PageId"]
            if section_id not in sections or not page_id or (section_id, page_id) in excluded:
                continue
            if (section_id, page_id) in seen:
                continue
            title = self.text(raw["Name"]) if raw["Name"] else ""
            if not title or title.startswith("LOC_"):
                continue
            seen.add((section_id, page_id))
            layout_id = raw["PageLayoutId"] or "Simple"
            page = Page(
                section_id=section_id,
                section=sections[section_id],
                page_id=page_id,
                layout=layouts.get(layout_id, layout_id),
                title=title,
                group=groups.get((section_id, raw["PageGroupId"]), "") if raw["PageGroupId"] else "",
                terms=terms.get((section_id, page_id), []),
                sort_index=raw["SortIndex"] or 0,
            )
            builder = LAYOUTS.get(page.layout)
            if builder:
                try:
                    builder(self, page)
                except (sqlite3.Error, KeyError, IndexError, TypeError, ValueError) as e:
                    log.warning("facts for %s failed: %s", page.uid, e)
            page.facts = [g for g in page.facts if g.lines]
            self._add_chapters(page, layout_id, raw.get("TextKeyPrefix"))
            if page.facts or page.chapters:  # a page with nothing on it cannot answer anything
                pages.append(page)
        return pages

    def _search_terms(self) -> dict[tuple[str, str], list[str]]:
        out: dict[tuple[str, str], list[str]] = {}
        rows = [(r["SectionId"], r["PageId"], r["Term"]) for r in self.q("SELECT * FROM CivilopediaPageSearchTerms")]
        for q in self.q("SELECT * FROM CivilopediaPageSearchTermQueries"):
            for r in self.q(q["SQL"]):
                keys = r.keys()
                if all(c in keys for c in (q["SectionIdColumn"], q["PageIdColumn"], q["SearchTermColumn"])):
                    rows.append((r[q["SectionIdColumn"]], r[q["PageIdColumn"]], r[q["SearchTermColumn"]]))
        for section_id, page_id, term in rows:
            t = self.text(term)
            if t and not t.startswith("LOC_"):
                out.setdefault((section_id, page_id), []).append(t)
        return out

    # -- chapters (CivilopediaSupport.lua) ---------------------------------------------------

    def _chapter_key(self, section_id, page_id, chapter_id, tag, prefix) -> str | None:
        suffix = f"_CHAPTER_{chapter_id}_{tag}"
        keys = [
            f"LOC_PEDIA_{section_id}_PAGE_{page_id}{suffix}",
            f"LOC_PEDIA_{section_id}_PAGE{suffix}",
            f"LOC_PEDIA_PAGE_{page_id}{suffix}",
            f"LOC_PEDIA_PAGE{suffix}",
        ]
        if prefix:
            keys[0:0] = [f"{prefix}_{page_id}{suffix}", f"{prefix}{suffix}"]
        return next((k for k in keys if self.loc.has(k)), None)

    def _add_chapters(self, page: Page, layout_id: str, prefix: str | None) -> None:
        chapters = self.q(
            "SELECT ChapterId FROM CivilopediaPageLayoutChapters WHERE PageLayoutId = ? ORDER BY SortIndex",
            layout_id,
        )
        sid, pid = page.section_id, page.page_id
        for r in chapters:
            cid = r["ChapterId"]
            header_rows = self.q(
                "SELECT Header FROM CivilopediaPageChapterHeaders WHERE SectionId=? AND PageId=? AND ChapterId=?",
                sid,
                pid,
                cid,
            )
            header_key = header_rows[0]["Header"] if header_rows else self._chapter_key(sid, pid, cid, "TITLE", prefix)
            para_rows = self.q(
                "SELECT Paragraph FROM CivilopediaPageChapterParagraphs WHERE SectionId=? AND PageId=? "
                "AND ChapterId=? ORDER BY SortIndex",
                sid,
                pid,
                cid,
            )
            keys = [p["Paragraph"] for p in para_rows]
            if not keys:
                body = self._chapter_key(sid, pid, cid, "BODY", prefix)
                if body:
                    keys = [body]
                else:
                    i = 1
                    while True:
                        k = self._chapter_key(sid, pid, cid, f"PARA_{i}", prefix)
                        if not k:
                            break
                        keys.append(k)
                        i += 1
            paragraphs = [t for t in (self.text(k) for k in keys) if t]
            if paragraphs:
                title = self.text(header_key) if header_key else ""
                if not header_key and cid == "HISTORY":
                    title = (
                        self.text("LOC_UI_PEDIA_HISTORICAL_CONTEXT")
                        if self.loc.has("LOC_UI_PEDIA_HISTORICAL_CONTEXT")
                        else "Historical Context"
                    )
                page.chapters.append(Chapter(title, paragraphs))

    def description(self, page: Page, key) -> None:
        if key and self.loc.has(key):
            text = self.text(key)
            if text:
                page.chapters.insert(0, Chapter(self.text("LOC_UI_PEDIA_DESCRIPTION"), [text]))

    # -- shared stat helpers ---------------------------------------------------------------

    def unique_to(self, trait_type) -> list[str]:
        if not trait_type:
            return []
        city_states = {
            r["CivilizationType"]
            for r in self.q(
                "SELECT CivilizationType FROM Civilizations WHERE StartingCivilizationLevelType = 'CIVILIZATION_LEVEL_CITY_STATE'"
            )
        }
        cs_leaders = {
            r["LeaderType"]: r["CivilizationType"]
            for r in self.q("SELECT * FROM CivilizationLeaders")
            if r["CivilizationType"] in city_states
        }
        out = []
        for r in self.q("SELECT LeaderType FROM LeaderTraits WHERE TraitType = ?", trait_type):
            civ = cs_leaders.get(r["LeaderType"])
            out.append(
                self.name("Civilizations", "CivilizationType", civ)
                if civ
                else self.name("Leaders", "LeaderType", r["LeaderType"])
            )
        for r in self.q("SELECT CivilizationType FROM CivilizationTraits WHERE TraitType = ?", trait_type):
            out.append(self.name("Civilizations", "CivilizationType", r["CivilizationType"]))
        return _unique([o for o in out if o])

    def trait_owners(self, trait_type, expand: bool = True) -> set[str]:
        """Civilization and leader types a trait belongs to (with each one's leaders or civilizations if expand)."""
        if not trait_type:
            return set()
        out = set()
        for r in self.q("SELECT LeaderType FROM LeaderTraits WHERE TraitType = ?", trait_type):
            out.add(r["LeaderType"])
            if expand:
                out |= {
                    c["CivilizationType"]
                    for c in self.q("SELECT * FROM CivilizationLeaders WHERE LeaderType = ?", r["LeaderType"])
                }
        for r in self.q("SELECT CivilizationType FROM CivilizationTraits WHERE TraitType = ?", trait_type):
            out.add(r["CivilizationType"])
            if expand:
                out |= {
                    c["LeaderType"]
                    for c in self.q("SELECT * FROM CivilizationLeaders WHERE CivilizationType = ?", r["CivilizationType"])
                }
        return out

    def beliefs_granting(self, effect: str, arg_name: str, value: str) -> list[str]:
        rows = self.q(
            "SELECT DISTINCT b.Name FROM Beliefs b "
            "JOIN BeliefModifiers bm ON bm.BeliefType = b.BeliefType "
            "JOIN Modifiers m ON m.ModifierId = bm.ModifierID "
            "JOIN DynamicModifiers d ON d.ModifierType = m.ModifierType "
            "JOIN ModifierArguments a ON a.ModifierId = m.ModifierId "
            "WHERE d.EffectType = ? AND a.Name = ? AND a.Value = ?",
            effect,
            arg_name,
            value,
        )
        return [self.text(r["Name"]) for r in rows]

    def yield_line(self, key, amount, yield_type, *extra) -> str:
        y = self.yield_row(yield_type)
        if y is None:
            return ""
        return self.text(key, amount, y["IconString"], y["Name"], *extra)

    def cost_line(self, amount, yield_type) -> str:
        return self.yield_line("LOC_UI_PEDIA_BASE_COST", amount, yield_type)

    def purchase_cost(self, row) -> tuple[float, str] | None:
        purchase_yield = _col(row, "PurchaseYield")
        cost = _col(row, "Cost") or 0
        if not purchase_yield or not cost:
            return None
        other = self.param("GOLD_EQUIVALENT_OTHER_YIELDS", 1)
        if purchase_yield == "YIELD_GOLD":
            amount = cost * self.param("GOLD_PURCHASE_MULTIPLIER", 1) * other
        else:
            amount = cost * other
        return amount, purchase_yield

    def adjacency_lines(self, link_table: str, owner_col: str, owner: str) -> list[str]:
        ids = {r["YieldChangeId"] for r in self.q(f"SELECT YieldChangeId FROM {link_table} WHERE {owner_col} = ?", owner)}
        lines = []
        for r in self.q("SELECT * FROM Adjacency_YieldChanges"):
            if r["ID"] not in ids:
                continue
            obj = None
            resource_class = _col(r, "AdjacentResourceClass")
            if _col(r, "OtherDistrictAdjacent"):
                obj = "LOC_TYPE_TRAIT_ADJACENT_OBJECT_DISTRICT"
            elif _col(r, "AdjacentResource"):
                obj = "LOC_TYPE_TRAIT_ADJACENT_OBJECT_RESOURCE"
            elif _col(r, "AdjacentSeaResource"):
                obj = "LOC_TYPE_TRAIT_ADJACENT_OBJECT_SEA_RESOURCE"
            elif resource_class and resource_class != "NO_RESOURCECLASS":
                obj = {
                    "RESOURCECLASS_BONUS": "LOC_TOOLTIP_BONUS_RESOURCE",
                    "RESOURCECLASS_LUXURY": "LOC_TOOLTIP_LUXURY_RESOURCE",
                    "RESOURCECLASS_STRATEGIC": "LOC_TOOLTIP_BONUS_STRATEGIC",
                    "RESOURCECLASS_LEY_LINE": "LOC_TOOLTIP_LEY_LINE_RESOURCE",
                }.get(resource_class, "LOC_TYPE_TRAIT_ADJACENT_OBJECT_RESOURCE_CLASS")
            elif _col(r, "AdjacentRiver"):
                obj = "LOC_TYPE_TRAIT_ADJACENT_OBJECT_RIVER"
            elif _col(r, "AdjacentWonder"):
                obj = "LOC_TYPE_TRAIT_ADJACENT_OBJECT_WONDER"
            elif _col(r, "AdjacentNaturalWonder"):
                obj = "LOC_TYPE_TRAIT_ADJACENT_OBJECT_NATURAL_WONDER"
            elif _col(r, "AdjacentTerrain"):
                obj = _col(self.get("Terrains", "TerrainType", r["AdjacentTerrain"]), "Name")
            elif _col(r, "AdjacentFeature"):
                obj = _col(self.get("Features", "FeatureType", r["AdjacentFeature"]), "Name")
            elif _col(r, "AdjacentImprovement"):
                obj = _col(self.get("Improvements", "ImprovementType", r["AdjacentImprovement"]), "Name")
            elif _col(r, "AdjacentDistrict"):
                obj = _col(self.get("Districts", "DistrictType", r["AdjacentDistrict"]), "Name")
            y = self.yield_row(r["YieldType"])
            if not obj or y is None:
                continue
            tiles = _col(r, "TilesRequired") or 1
            key = "LOC_TYPE_TRAIT_ADJACENT_BONUS_PER" if tiles > 1 else "LOC_TYPE_TRAIT_ADJACENT_BONUS"
            if _col(r, "AdjacentRiver") and link_table == "District_Adjacencies":
                key = "LOC_TYPE_TRAIT_ADJACENT_BONUS_ONCE"
            value = self.loc.lookup(key, r["YieldChange"], y["IconString"], y["Name"], tiles, obj)
            prereq = self._tech_or_civic(_col(r, "PrereqCivic"), _col(r, "PrereqTech"))
            if prereq:
                value += " " + self.loc.lookup("LOC_TYPE_TRAIT_ADJACENT_BONUS_REQUIRES_TECH_OR_CIVIC", prereq).strip()
            obsolete = self._tech_or_civic(_col(r, "ObsoleteCivic"), _col(r, "ObsoleteTech"))
            if obsolete:
                value += " " + self.loc.lookup("LOC_TYPE_TRAIT_ADJACENT_BONUS_OBSOLETE_WITH_TECH_OR_CIVIC", obsolete).strip()
            lines.append(clean(value))
        return lines

    def _tech_or_civic(self, civic, tech):
        if civic:
            return _col(self.get("Civics", "CivicType", civic), "Name")
        if tech:
            return _col(self.get("Technologies", "TechnologyType", tech), "Name")
        return None

    def prereqs(self, box: _Box, row) -> None:
        for header, table, col, value in (
            ("LOC_DISTRICT_NAME", "Districts", "DistrictType", _col(row, "PrereqDistrict")),
            ("LOC_CIVIC_NAME", "Civics", "CivicType", _col(row, "PrereqCivic")),
            ("LOC_TECHNOLOGY_NAME", "Technologies", "TechnologyType", _col(row, "PrereqTech")),
        ):
            name = self.name(table, col, value)
            if name:
                box.header(header)
                box.line(name)

    def used_traits(self) -> set[str]:
        if self._used_traits is None:
            used = set()
            for table in ("Units", "Buildings", "Districts", "Improvements"):
                used |= {r["TraitType"] for r in self.q(f"SELECT TraitType FROM {table} WHERE TraitType IS NOT NULL")}
            self._used_traits = used
        return self._used_traits

    def trait_owned(self, link_table: str, owner_col: str, owners: list[str]) -> tuple[list[str], list[str]]:
        """(special units, special infrastructure) for civ or leader traits."""
        traits = set()
        for o in owners:
            traits |= {r["TraitType"] for r in self.q(f"SELECT TraitType FROM {link_table} WHERE {owner_col} = ?", o)}
        units = [
            self.text(r["Name"])
            for r in self.q("SELECT TraitType, Name FROM Units WHERE TraitType IS NOT NULL")
            if r["TraitType"] in traits
        ]
        infra = []
        for table in ("Buildings", "Districts", "Improvements"):
            infra += [
                self.text(r["Name"])
                for r in self.q(f"SELECT TraitType, Name FROM {table} WHERE TraitType IS NOT NULL")
                if r["TraitType"] in traits
            ]
        return _unique(units), _unique(infra)

    def unique_abilities(self, page: Page, link_table: str, owner_col: str, owners: list[str], filter_used=True) -> None:
        abilities = []
        seen = set()
        for o in owners:
            for r in self.q(
                f"SELECT t.TraitType, t.Name, t.Description, t.InternalOnly FROM {link_table} l "
                f"JOIN Traits t ON t.TraitType = l.TraitType WHERE l.{owner_col} = ?",
                o,
            ):
                if r["TraitType"] in seen or not r["Name"] or not r["Description"] or r["Name"] == "NONE":
                    continue
                if _col(r, "InternalOnly"):
                    continue
                if filter_used and r["TraitType"] in self.used_traits():
                    continue
                seen.add(r["TraitType"])
                abilities.append(Chapter(self.text(r["Name"]), [self.text(r["Description"])]))
        if abilities:
            heading = self.text("LOC_UI_PEDIA_UNIQUE_ABILITY")
            page.chapters[0:0] = [Chapter(f"{heading}: {a.title}", a.paragraphs) for a in abilities]


_NAMED_TYPES = [
    ("Yields", "YieldType"),
    ("Districts", "DistrictType"),
    ("Buildings", "BuildingType"),
    ("Units", "UnitType"),
    ("Improvements", "ImprovementType"),
    ("Technologies", "TechnologyType"),
    ("Civics", "CivicType"),
    ("Policies", "PolicyType"),
    ("Governments", "GovernmentType"),
    ("Resources", "ResourceType"),
    ("Beliefs", "BeliefType"),
    ("GreatPersonClasses", "GreatPersonClassType"),
]


def _col(row, name):
    if row is None:
        return None
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _number_or_text(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    return int(f) if f == int(f) else f


def _unique(items):
    seen = set()
    out = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _sorted_names(names):
    return sorted(_unique([n for n in names if n]), key=str.lower)


# -- page layouts ---------------------------------------------------------------------------


def district(p: Pedia, page: Page) -> None:
    d = p.get("Districts", "DistrictType", page.page_id)
    if d is None:
        return
    dt = d["DistrictType"]
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    unique = p.unique_to(_col(d, "TraitType"))
    if unique:
        traits.lines("LOC_UI_PEDIA_UNIQUE_TO", unique)
    replaces = [
        p.name("Districts", "DistrictType", r["ReplacesDistrictType"])
        for r in p.q("SELECT * FROM DistrictReplaces WHERE CivUniqueDistrictType = ?", dt)
    ]
    traits.lines("LOC_UI_PEDIA_REPLACES", replaces)
    replaced_by = [
        p.name("Districts", "DistrictType", r["CivUniqueDistrictType"])
        for r in p.q("SELECT * FROM DistrictReplaces WHERE ReplacesDistrictType = ?", dt)
    ]
    traits.lines("LOC_UI_PEDIA_REPLACED_BY", replaced_by)
    traits.header("LOC_UI_PEDIA_TRAITS")
    for r in p.q(
        "SELECT GreatPersonClassType, SUM(PointsPerTurn) AS n FROM District_GreatPersonPoints "
        "WHERE DistrictType = ? GROUP BY GreatPersonClassType",
        dt,
    ):
        gp = p.get("GreatPersonClasses", "GreatPersonClassType", r["GreatPersonClassType"])
        if gp is not None:
            traits.line("LOC_TYPE_TRAIT_GREAT_PERSON_POINTS", r["n"], gp["IconString"], gp["Name"])
    for col, key in (
        ("Housing", "LOC_TYPE_TRAIT_HOUSING"),
        ("Entertainment", "LOC_TYPE_TRAIT_AMENITY_ENTERTAINMENT"),
        ("CitizenSlots", "LOC_TYPE_TRAIT_CITIZENS"),
        ("AirSlots", "LOC_TYPE_TRAIT_AIRSLOTS"),
        ("Appeal", "LOC_TYPE_TRAIT_APPEAL"),
    ):
        if _col(d, col):
            traits.line(key, d[col])
    plunder = {
        "PLUNDER_CULTURE": "YIELD_CULTURE",
        "PLUNDER_SCIENCE": "YIELD_SCIENCE",
        "PLUNDER_FAITH": "YIELD_FAITH",
        "PLUNDER_GOLD": "YIELD_GOLD",
    }.get(_col(d, "PlunderType"))
    if plunder:
        y = p.yield_row(plunder)
        traits.line("LOC_TYPE_TRAIT_PILLAGE_AWARD", y["IconString"], y["Name"])
    traits.lines("LOC_UI_PEDIA_ADJACENCY_BONUS", p.adjacency_lines("District_Adjacencies", "DistrictType", dt))
    traits.lines(
        "LOC_UI_PEDIA_CITIZEN_YIELDS",
        [
            p.yield_line("LOC_TYPE_TRAIT_YIELD", r["YieldChange"], r["YieldType"])
            for r in p.q("SELECT * FROM District_CitizenYieldChanges WHERE DistrictType = ?", dt)
        ],
    )
    trade = []
    for label, col in (
        ("LOC_UI_PEDIA_TRADE_ORIGIN", "YieldChangeAsOrigin"),
        ("LOC_UI_PEDIA_TRADE_DOMESTIC_DESTINATION", "YieldChangeAsDomesticDestination"),
        ("LOC_UI_PEDIA_TRADE_INTERNATIONAL_DESTINATION", "YieldChangeAsInternationalDestination"),
    ):
        parts = []
        for r in p.q(
            f"SELECT YieldType, SUM({col}) AS n FROM District_TradeRouteYields WHERE DistrictType = ? GROUP BY YieldType", dt
        ):
            if r["n"]:
                y = p.yield_row(r["YieldType"])
                parts.append(p.text("{1: number +#;-#} {2_Icon} {3_Name}", r["n"], y["IconString"], y["Name"]))
        if parts:
            trade.append(f"{p.text(label)} {', '.join(parts)}")
    traits.lines("LOC_UI_PEDIA_TRADE_YIELDS", trade)

    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    p.prereqs(req, d)
    placement = []
    if _col(d, "Coast"):
        placement.append(p.text("LOC_UI_PEDIA_PLACEMENT_ADJ_TO_COAST"))
    if _col(d, "Aqueduct"):
        placement.append(p.text("LOC_UI_PEDIA_PLACEMENT_ADJ_TO_CITY"))
    if _col(d, "NoAdjacentCity"):
        placement.append(p.text("LOC_UI_PEDIA_PLACEMENT_NOT_ADJ_TO_CITY"))
    placement += _sorted_names(
        [
            p.name("Features", "FeatureType", r["FeatureType"])
            for r in p.q("SELECT * FROM District_RequiredFeatures WHERE DistrictType = ?", dt)
        ]
        + [
            p.name("Terrains", "TerrainType", r["TerrainType"])
            for r in p.q("SELECT * FROM District_ValidTerrains WHERE DistrictType = ?", dt)
        ]
    )
    req.lines("LOC_UI_PEDIA_PLACEMENT", placement)
    req.header("LOC_UI_PEDIA_PRODUCTION_COST")
    req.line(p.cost_line(d["Cost"], "YIELD_PRODUCTION"))
    if _col(d, "Maintenance"):
        req.header("LOC_UI_PEDIA_MAITENANCE_COST")
        req.line(p.cost_line(d["Maintenance"], "YIELD_GOLD"))

    usage = _Box(page, p.loc, "LOC_UI_PEDIA_USAGE")
    match = {dt} | {r["ReplacesDistrictType"] for r in p.q("SELECT * FROM DistrictReplaces WHERE CivUniqueDistrictType = ?", dt)}
    buildings = [r for r in p.q("SELECT * FROM Buildings WHERE InternalOnly = 0") if r["PrereqDistrict"] in match]
    if unique:
        buildings = [b for b in buildings if not _col(b, "TraitType") or set(p.unique_to(b["TraitType"])) & set(unique)]
    usage.lines("LOC_UI_PEDIA_USAGE_UNLOCKS_BUILDINGS", _sorted_names(p.text(b["Name"]) for b in buildings))
    usage.lines(
        "LOC_UI_PEDIA_USAGE_UNLOCKS_UNITS",
        _sorted_names(p.text(r["Name"]) for r in p.q("SELECT * FROM Units") if _col(r, "PrereqDistrict") in match),
    )
    p.description(page, _col(d, "Description"))


def building(p: Pedia, page: Page) -> None:
    b = p.get("Buildings", "BuildingType", page.page_id)
    if b is None:
        return
    bt = b["BuildingType"]
    xp2 = p.get("Buildings_XP2", "BuildingType", bt)
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    unique = p.unique_to(_col(b, "TraitType"))
    if _col(b, "EnabledByReligion"):
        unique += p.beliefs_granting("EFFECT_ADD_RELIGIOUS_BUILDING", "BuildingType", bt)
    traits.lines("LOC_UI_PEDIA_UNIQUE_TO", unique)
    traits.lines(
        "LOC_UI_PEDIA_REPLACES",
        [
            p.name("Buildings", "BuildingType", r["ReplacesBuildingType"])
            for r in p.q("SELECT * FROM BuildingReplaces WHERE CivUniqueBuildingType = ?", bt)
        ],
    )
    traits.lines(
        "LOC_UI_PEDIA_REPLACED_BY",
        [
            p.name("Buildings", "BuildingType", r["CivUniqueBuildingType"])
            for r in p.q("SELECT * FROM BuildingReplaces WHERE ReplacesBuildingType = ?", bt)
        ],
    )
    era = _col(b, "ObsoleteEra")
    if era and era != "NO_ERA":
        wonder = _col(b, "MaxWorldInstances") not in (None, -1)
        traits.lines(
            "LOC_UI_PEDIA_MADE_REMOVED_IF" if wonder else "LOC_UI_PEDIA_MADE_OBSOLETE_BY", [p.name("Eras", "EraType", era)]
        )
    traits.header("LOC_UI_PEDIA_TRAITS")
    for r in p.q("SELECT * FROM Building_YieldChanges WHERE BuildingType = ?", bt):
        traits.line(p.yield_line("LOC_TYPE_TRAIT_YIELD", r["YieldChange"], r["YieldType"]))
    for r in p.q("SELECT * FROM Building_YieldChangesBonusWithPower WHERE BuildingType = ?", bt):
        traits.line(p.yield_line("LOC_TYPE_TRAIT_YIELD_POWER_ENHANCEMENT", r["YieldChange"], r["YieldType"]))
    for r in p.q("SELECT * FROM Building_YieldDistrictCopies WHERE BuildingType = ?", bt):
        to, frm = p.yield_row(r["NewYieldType"]), p.yield_row(r["OldYieldType"])
        if to is not None and frm is not None:
            traits.line("LOC_TYPE_TRAIT_BUILDING_DISTRICT_COPY", to["IconString"], to["Name"], frm["IconString"], frm["Name"])
    for col, key in (("Housing", "LOC_TYPE_TRAIT_HOUSING"), ("Entertainment", "LOC_TYPE_TRAIT_AMENITY_ENTERTAINMENT")):
        if _col(b, col):
            traits.line(key, b[col])
    if _col(xp2, "EntertainmentBonusWithPower"):
        traits.line("LOC_TYPE_TRAIT_AMENITY_ENTERTAINMENT_POWER_ENHANCEMENT", xp2["EntertainmentBonusWithPower"])
    for col, key in (("CitizenSlots", "LOC_TYPE_TRAIT_CITIZENS"), ("OuterDefenseHitPoints", "LOC_TYPE_TRAIT_OUTER_DEFENSE")):
        if _col(b, col):
            traits.line(key, b[col])
    for r in p.q("SELECT * FROM Building_GreatPersonPoints WHERE BuildingType = ?", bt):
        gp = p.get("GreatPersonClasses", "GreatPersonClassType", r["GreatPersonClassType"])
        if gp is not None:
            traits.line("LOC_TYPE_TRAIT_GREAT_PERSON_POINTS", r["PointsPerTurn"], gp["IconString"], gp["Name"])
    for r in p.q("SELECT * FROM Building_GreatWorks WHERE BuildingType = ?", bt):
        slot = (r["GreatWorkSlotType"] or "").replace("GREATWORKSLOT_", "")
        traits.line(f"LOC_TYPE_TRAIT_GREAT_WORKS_{slot}_SLOTS", r["NumSlots"])
    bombs = sum(r["TourismBombValue"] or 0 for r in p.q("SELECT * FROM Building_TourismBombs_XP2 WHERE BuildingType = ?", bt))
    if _col(b, "MaxWorldInstances") not in (None, -1):
        bombs += p.param("TOURISM_BOMB_WONDER_ADDITIONAL")  # every wonder, with or without a row of its own
    if bombs:
        traits.line("LOC_UI_PEDIA_TOURISM_BOMB_VALUE_BUILDING", bombs)
    traits.lines(
        "LOC_UI_PEDIA_CITIZEN_YIELDS",
        [
            p.yield_line("LOC_TYPE_TRAIT_YIELD", r["YieldChange"], r["YieldType"])
            for r in p.q("SELECT * FROM Building_CitizenYieldChanges WHERE BuildingType = ?", bt)
        ],
    )

    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    p.prereqs(req, b)
    prereq_buildings = [
        p.name("Buildings", "BuildingType", r["PrereqBuilding"])
        for r in p.q("SELECT * FROM BuildingPrereqs WHERE Building = ?", bt)
    ]
    req.lines("LOC_UI_PEDIA_REQUIRED_BUILDINGS_OR" if len(prereq_buildings) > 1 else "LOC_BUILDING_NAME", prereq_buildings)
    if _col(b, "RequiresReligion"):
        req.header("LOC_UI_PEDIA_REQUIREMENTS")
        req.line("LOC_UI_PEDIA_REQUIRES_FOUND_RELIGION")
    req.lines(
        "LOC_UI_PEDIA_EXCLUSIVE_WITH",
        [
            p.name("Buildings", "BuildingType", r["MutuallyExclusiveBuilding"])
            for r in p.q("SELECT * FROM MutuallyExclusiveBuildings WHERE Building = ?", bt)
        ],
    )
    adjacency = [
        p.name("Districts", "DistrictType", _col(b, "AdjacentDistrict")),
        p.name("Improvements", "ImprovementType", _col(b, "AdjacentImprovement")),
        p.name("Resources", "ResourceType", _col(b, "AdjacentResource")),
    ]
    adjacency += sorted(
        t
        for t in (
            p.text("LOC_UI_PEDIA_PLACEMENT_REQUIRES_ADJACENT_RIVER") if _col(b, "RequiresRiver") else "",
            p.text("LOC_UI_PEDIA_PLACEMENT_REQUIRES_ADJACENT_MOUNTAIN") if _col(b, "AdjacentToMountain") else "",
        )
        if t
    )
    req.lines("LOC_UI_PEDIA_ADJACENCY", adjacency)
    coastal = _col(b, "Coast") or _col(b, "MustBeAdjacentLand")
    places = [
        p.name("Features", "FeatureType", r["FeatureType"])
        for r in p.q("SELECT * FROM Building_RequiredFeatures WHERE BuildingType = ?", bt)
    ]
    places += [
        p.name("Features", "FeatureType", r["FeatureType"])
        for r in p.q("SELECT * FROM Building_ValidFeatures WHERE BuildingType = ?", bt)
    ]
    places += [
        p.name("Terrains", "TerrainType", r["TerrainType"])
        for r in p.q("SELECT * FROM Building_ValidTerrains WHERE BuildingType = ?", bt)
        if not (coastal and r["TerrainType"] == "TERRAIN_COAST")
    ]
    flags = sorted(
        t
        for t in (
            p.text("LOC_UI_PEDIA_PLACEMENT_REQUIRES_LAKE") if _col(b, "MustBeLake") else "",
            p.text("LOC_UI_PEDIA_PLACEMENT_REQUIRES_NOT_LAKE") if _col(b, "MustNotBeLake") else "",
            p.text("LOC_UI_PEDIA_PLACEMENT_REQUIRES_COAST") if coastal else "",
        )
        if t
    )
    req.lines("LOC_UI_PEDIA_PLACEMENT", _sorted_names(places) + flags)
    if _col(b, "Cost"):
        if not _col(b, "MustPurchase"):
            req.header("LOC_UI_PEDIA_PRODUCTION_COST")
            req.line(p.cost_line(b["Cost"], "YIELD_PRODUCTION"))
        purchase = p.purchase_cost(b)
        if purchase:
            req.header("LOC_UI_PEDIA_PURCHASE_COST")
            req.line(p.cost_line(*purchase))
    if _col(b, "Maintenance"):
        req.header("LOC_UI_PEDIA_MAITENANCE_COST")
        req.line(p.cost_line(b["Maintenance"], "YIELD_GOLD"))
    if _col(xp2, "RequiredPower"):
        req.header("LOC_UI_PEDIA_POWER_COST")
        req.line("LOC_UI_PEDIA_BASE_POWER_LOAD", xp2["RequiredPower"])

    # Better Civilopedia's extra facts.
    more = []
    if (_col(b, "OuterDefenseStrength") or 0) > 0:
        more.append(f"{p.text('LOC_COMBAT_PREVIEW_BASE_STRENGTH')}: {p.text('+{1} [ICON_Strength]', b['OuterDefenseStrength'])}")
    if (_col(b, "RegionalRange") or 0) > 0:
        more.append(f"{p.text('LOC_UI_PEDIA_RANGE')}: {_num(b['RegionalRange'])}")
    for r in p.q("SELECT * FROM Building_GreatWorks WHERE BuildingType = ?", bt):
        more.append(p.text(_col(r, "ThemingBonusDescription")))
    for r in p.q("SELECT * FROM Building_YieldsPerEra WHERE BuildingType = ?", bt):
        more.append(p.yield_line("LOC_TYPE_TRAIT_YIELD", r["YieldChange"], r["YieldType"]) + f" ({p.text('LOC_ERA_NAME')})")
    tier = _col(b, "GovernmentTierRequirement")
    if tier:
        more += [
            p.text("LOC_TOOLTIP_UNLOCKS_GOVERNMENT", g["Name"]) for g in p.q("SELECT Name FROM Governments WHERE Tier = ?", tier)
        ]
    trait_heading = p.text("LOC_UI_PEDIA_TRAITS")
    group = next((g for g in reversed(page.facts) if g.heading == trait_heading), None)
    if group is None:
        group = FactGroup(trait_heading)
        page.facts.insert(0, group)
    group.lines += [m for m in more if m]
    more = _Box(page, p.loc, "LOC_UI_PEDIA_USAGE")
    more.lines(
        "LOC_UI_PEDIA_USAGE_UNLOCKS_BUILDINGS",
        _sorted_names(
            p.name("Buildings", "BuildingType", r["Building"])
            for r in p.q("SELECT * FROM BuildingPrereqs WHERE PrereqBuilding = ?", bt)
        ),
    )
    more.lines(
        "LOC_UI_PEDIA_USAGE_UNLOCKS_UNITS",
        _sorted_names(
            p.name("Units", "UnitType", r["Unit"]) for r in p.q("SELECT * FROM Unit_BuildingPrereqs WHERE PrereqBuilding = ?", bt)
        ),
    )
    p.description(page, _col(b, "Description"))


def unit(p: Pedia, page: Page) -> None:
    u = p.get("Units", "UnitType", page.page_id)
    if u is None:
        return
    ut = u["UnitType"]
    xp2 = p.get("Units_XP2", "UnitType", ut)
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    unique = p.unique_to(_col(u, "TraitType")) + p.beliefs_granting("EFFECT_ADD_RELIGIOUS_UNIT", "UnitType", ut)
    traits.lines("LOC_UI_PEDIA_UNIQUE_TO", unique)
    traits.lines(
        "LOC_UI_PEDIA_REPLACES",
        [
            p.name("Units", "UnitType", r["ReplacesUnitType"])
            for r in p.q("SELECT * FROM UnitReplaces WHERE CivUniqueUnitType = ?", ut)
        ],
    )
    traits.lines(
        "LOC_UI_PEDIA_REPLACED_BY",
        [
            p.name("Units", "UnitType", r["CivUniqueUnitType"])
            for r in p.q("SELECT * FROM UnitReplaces WHERE ReplacesUnitType = ?", ut)
        ],
    )
    owners = p.trait_owners(_col(u, "TraitType"), expand=False)

    def matches_unique(other) -> bool:
        # On a unique unit's page, other civilizations' unique units are left out of the upgrade path.
        if other is None or _col(other, "TraitType") == "TRAIT_BARBARIAN":
            return False
        return not owners or not _col(other, "TraitType") or bool(owners & p.trait_owners(other["TraitType"]))

    replaced_by = p.q("SELECT * FROM UnitReplaces")
    upgrades_to, upgrades_from = [], []
    for r in p.q("SELECT * FROM UnitUpgrades"):
        if r["Unit"] == ut:
            target = p.get("Units", "UnitType", r["UpgradeUnit"])
            if target is not None and _col(target, "TraitType") != "TRAIT_BARBARIAN":
                if matches_unique(target):
                    upgrades_to.append(target)
                upgrades_to += [
                    v
                    for v in (p.get("Units", "UnitType", x["CivUniqueUnitType"]) for x in replaced_by)
                    if v is not None and replaces_type(replaced_by, v["UnitType"]) == target["UnitType"] and matches_unique(v)
                ]
        if r["UpgradeUnit"] == ut or r["UpgradeUnit"] == replaces_type(replaced_by, ut):
            source = p.get("Units", "UnitType", r["Unit"])
            if matches_unique(source):
                upgrades_from.append(source)
    traits.lines("LOC_UI_PEDIA_UPGRADES_TO", _unique(p.text(x["Name"]) for x in upgrades_to))
    traits.lines("LOC_UI_PEDIA_UPGRADE_FROM", _unique(p.text(x["Name"]) for x in upgrades_from))
    obsolete = p._tech_or_civic(_col(u, "ObsoleteCivic"), _col(u, "ObsoleteTech"))
    traits.lines("LOC_UI_PEDIA_MADE_OBSOLETE_BY", [p.text(obsolete)] if obsolete else [])
    traits.header("LOC_UI_PEDIA_TRAITS")
    promo = p.name("UnitPromotionClasses", "PromotionClassType", _col(u, "PromotionClass"))
    if promo and page.layout != "HeroUnit":
        traits.line("LOC_UNIT_PROMOTION_CLASS", promo)
    stats = [
        ("BaseMoves", "LOC_UI_PEDIA_MOVEMENT_POINTS"),
        ("Combat", "LOC_UI_PEDIA_MELEE_STRENGTH"),
        ("RangedCombat", "LOC_UI_PEDIA_RANGED_STRENGTH"),
        ("Bombard", "LOC_UI_PEDIA_BOMBARD_STRENGTH"),
        ("ReligiousStrength", "LOC_UI_PEDIA_RELIGIOUS_STRENGTH"),
        ("AntiAirCombat", "LOC_UI_PEDIA_ANTIAIR_STRENGTH"),
        ("Range", "LOC_UI_PEDIA_RANGE"),
        ("SpreadCharges", "LOC_UI_PEDIA_SPREAD_CHARGES"),
        ("BuildCharges", "LOC_UI_PEDIA_BUILD_CHARGES"),
        ("ReligiousHealCharges", "LOC_UI_PEDIA_HEAL_CHARGES"),
    ]
    if u["UnitType"] == "UNIT_SPY" and p.param("ESPIONAGE_MAX_LEVEL") > 1:
        traits.line("LOC_TYPE_TRAIT_MAX_PROMOTIONS", p.param("ESPIONAGE_MAX_LEVEL") - 1)
    for col, key in stats:
        value = _col(u, col)
        if col == "BaseMoves" and _col(u, "IgnoreMoves") and _col(u, "Domain") != "DOMAIN_AIR":
            continue  # units that move by teleporting (spies, traders) show no movement
        if value:
            traits.line(f"{p.text(key)}: {_num(value)}")
    if _col(u, "AirSlots"):
        traits.line("LOC_TYPE_TRAIT_AIRSLOTS", u["AirSlots"])
    if _col(xp2, "TourismBombPossible"):
        traits.line("LOC_UI_PEDIA_UNIT_ROCK_BAND_TOURISM_WONDERS", p.param("TOURISM_BOMB_WONDER_ADDITIONAL"))
    if _col(u, "CanEarnExperience") == 0 or _col(xp2, "CanEarnExperience") == 0:
        traits.line("LOC_TYPE_TRAIT_CANNOT_EARN_EXPERIENCE")

    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    p.prereqs(req, u)
    req.lines(
        "LOC_BUILDING_NAME",
        [
            p.name("Buildings", "BuildingType", r["PrereqBuilding"])
            for r in p.q("SELECT * FROM Unit_BuildingPrereqs WHERE Unit = ?", ut)
        ],
    )
    if _col(u, "CanTrain") != 0 and not _col(u, "MustPurchase") and _col(u, "Cost"):
        req.header("LOC_UI_PEDIA_PRODUCTION_COST")
        req.line(p.cost_line(u["Cost"], "YIELD_PRODUCTION"))
    resource = _col(u, "StrategicResource")
    if resource and _col(xp2, "ResourceCost"):
        req.line(
            "LOC_UI_PEDIA_RESOURCE_COST",
            xp2["ResourceCost"],
            f"[ICON_{resource}]",
            _col(p.get("Resources", "ResourceType", resource), "Name"),
        )
    purchase = p.purchase_cost(u)
    if purchase:
        req.header("LOC_UI_PEDIA_PURCHASE_COST")
        req.line(p.cost_line(*purchase))
    if _col(u, "Maintenance"):
        req.header("LOC_UI_PEDIA_MAITENANCE_COST")
        req.line(p.cost_line(u["Maintenance"], "YIELD_GOLD"))
    maint_res = _col(xp2, "ResourceMaintenanceType")
    if maint_res and _col(xp2, "ResourceMaintenanceAmount"):
        req.line(
            "LOC_UI_PEDIA_RESOURCE_MAINTENANCE_AMOUNT",
            xp2["ResourceMaintenanceAmount"],
            f"[ICON_{maint_res}]",
            _col(p.get("Resources", "ResourceType", maint_res), "Name"),
        )

    if _col(xp2, "TourismBombPossible"):
        concerts = _Box(page, p.loc, "LOC_UI_PEDIA_UNIT_ROCK_BAND_TOURISM_BUILDING_HEADER")
        for r in p.q("SELECT * FROM Building_TourismBombs_XP2"):
            name = p.name("Buildings", "BuildingType", r["BuildingType"])
            if name:
                concerts.line(f"+{_num(r['TourismBombValue'])} {p.text('LOC_UI_PEDIA_UNIT_ROCK_BAND_TOURISM_BUILDING', name)}")

    usage = _Box(page, p.loc, "LOC_UI_PEDIA_USAGE")
    usage.lines(
        "LOC_UI_PEDIA_USAGE_CAN_CONSTRUCT",
        _sorted_names(
            [
                p.name("Improvements", "ImprovementType", r["ImprovementType"])
                for r in p.q("SELECT * FROM Improvement_ValidBuildUnits WHERE UnitType = ?", ut)
            ]
            + [
                p.name("Routes", "RouteType", r["RouteType"])
                for r in p.q("SELECT * FROM Route_ValidBuildUnits WHERE UnitType = ?", ut)
            ]
        ),
    )
    if _col(u, "Spy"):
        missions = _Box(page, p.loc, "LOC_UI_PEDIA_ESPIONAGE_MISSIONS")
        for r in p.q("SELECT * FROM UnitOperations WHERE InterfaceMode = 'INTERFACEMODE_SPY_CHOOSE_MISSION'"):
            parts = []
            if (_col(r, "Turns") or 0) > 0:
                parts.append(p.text("LOC_TYPE_TRAIT_TURNS", r["Turns"]))
            if (_col(r, "BaseProbability") or 0) > 0:
                parts.append(p.text("LOC_TYPE_TRAIT_BASE_PROBABILITY", _three_dice_odds(r["BaseProbability"])))
            if _col(r, "TargetDistrict"):
                parts.append(p.text("LOC_TYPE_TRAIT_TARGET_DISTRICT", p.name("Districts", "DistrictType", r["TargetDistrict"])))
            name = p.text(r["Description"])
            missions.line(f"{name} ({'; '.join(parts)})" if parts else name)
    if page.layout == "HeroUnit":
        hero_abilities(p, page, ut)
    great_person_sources(p, page, ut)
    p.description(page, _col(u, "Description"))


def great_person_sources(p: Pedia, page: Page, unit_type: str) -> None:
    """Better Civilopedia: on a great person's unit page (Great Scientist...), where its points come from."""
    gp = next(iter(p.q("SELECT * FROM GreatPersonClasses WHERE UnitType = ?", unit_type)), None)
    if gp is None:
        return
    cls = gp["GreatPersonClassType"]

    def per_turn(n) -> str:
        return p.text("LOC_TYPE_TRAIT_GREAT_PERSON_POINTS", n, gp["IconString"], gp["Name"])

    box = _Box(page, p.loc, "LOC_PEDIA_DISTRICTS_TITLE")
    for r in p.q("SELECT * FROM District_GreatPersonPoints WHERE GreatPersonClassType = ?", cls):
        box.line(f"{p.name('Districts', 'DistrictType', r['DistrictType'])}: {per_turn(r['PointsPerTurn'])}")
    buildings = p.q(
        "SELECT b.Name, b.IsWonder, g.PointsPerTurn FROM Building_GreatPersonPoints g "
        "JOIN Buildings b ON b.BuildingType = g.BuildingType WHERE g.GreatPersonClassType = ?",
        cls,
    )
    for heading, wonder in (("LOC_PEDIA_BUILDINGS_TITLE", False), ("LOC_PEDIA_WONDERS_PAGEGROUP_WONDERS_NAME", True)):
        lines = [f"{p.text(r['Name'])}: {per_turn(r['PointsPerTurn'])}" for r in buildings if bool(r["IsWonder"]) == wonder]
        box.lines(heading, lines)
    box.lines(
        "LOC_PEDIA_WONDERS_PAGEGROUP_PROJECTS_NAME",
        [
            f"{p.name('Projects', 'ProjectType', r['ProjectType'], 'ShortName')}: {_num(r['Points'])} {p.text(gp['Name'])} points"
            for r in p.q("SELECT * FROM Project_GreatPersonPoints WHERE GreatPersonClassType = ?", cls)
        ],
    )


def replaces_type(replaces, unit_type) -> str | None:
    return next((r["ReplacesUnitType"] for r in replaces if r["CivUniqueUnitType"] == unit_type), None)


_THREE_DICE = [0, 0, 1, 3, 6, 10, 15, 21, 25, 27, 27, 25, 21, 15, 10, 6, 3, 1]  # ways to roll 1..18 on three dice


def _three_dice_odds(at_least: int) -> int:
    """A spy mission's chance as the game's Civilopedia shows it: rolling at least N on three dice."""
    ways = sum(_THREE_DICE[i - 1] for i in range(max(1, int(at_least)), 19))
    return int(ways / 216 * 100 + 0.5)


# Heroes mode: abilities each hero has, hard-coded in the game's HeroesSupport.lua.
_HERO_ABILITIES = {
    "HEROCLASS_ANANSI": ["ABILITY_HERO_IGNORE_FOREST_TERRAIN_COST"],
    "HEROCLASS_BEOWULF": ["ABILITY_HERO_IGNORE_HILLS_TERRAIN_COST"],
    "HEROCLASS_HERCULES": ["ABILITY_HERO_IGNORE_HILLS_TERRAIN_COST"],
    "HEROCLASS_HIMIKO": [
        "ABILITY_HERO_COMBAT_STRENGTH_AOE",
        "ABILITY_HERO_ENTER_FOREIGN_LANDS",
        "ABILITY_HERO_IGNORE_ALL_TERRAIN_COST",
    ],
    "HEROCLASS_HIPPOLYTA": ["ABILITY_HIPPOLYTA_HEAL_PER_TURN", "ABILITY_HERO_IGNORE_HILLS_TERRAIN_COST"],
    "HEROCLASS_HUNAHPU": ["ABILITY_HUNAHPU_RESURRECT_KILL", "ABILITY_HERO_IGNORE_FOREST_TERRAIN_COST"],
    "HEROCLASS_OYA": ["ABILITY_HERO_IGNORE_ALL_TERRAIN_COST"],
    "HEROCLASS_MAUI": ["ABILITY_HERO_IGNORE_FOREST_TERRAIN_COST"],
    "HEROCLASS_MULAN": ["ABILITY_MULAN_STRENGTH_PER_TURN", "ABILITY_MULAN_FORTIFY"],
    "HEROCLASS_SINBAD": ["ABILITY_SINBAD_GOLD_FOR_DISCOVERY", "ABILITY_SINBAD_OCEAN_VALID_TERRAIN"],
    "HEROCLASS_WUKONG": ["ABILITY_WUKONG_STEALTH", "ABILITY_WUKONG_LIFESPAN", "ABILITY_HERO_IGNORE_ALL_TERRAIN_COST"],
}


def hero_abilities(p: Pedia, page: Page, unit_type: str) -> None:
    hero = next(iter(p.q("SELECT * FROM HeroClasses WHERE UnitType = ?", unit_type)), None)
    if hero is None:
        return
    lines = []
    for ability in _HERO_ABILITIES.get(hero["HeroClassType"], []):
        a = p.get("UnitAbilities", "UnitAbilityType", ability)
        if a is not None:
            lines.append(f"{p.text(a['Name'])}: {p.text(_col(a, 'Description'))}")
    for r in p.q(
        "SELECT c.Description, c.Help FROM HeroClassUnitCommands h JOIN UnitCommands c ON c.CommandType = h.UnitCommandType "
        "WHERE h.HeroClassType = ?",
        hero["HeroClassType"],
    ):
        lines.append(f"{p.text(r['Description'])}: {p.text(r['Help'])}")
    if lines:
        page.chapters.insert(0, Chapter(p.text("LOC_PEDIA_PAGE_CHAPTER_HEROABILITIES_TITLE"), ["\n".join(lines)]))


def improvement(p: Pedia, page: Page) -> None:
    i = p.get("Improvements", "ImprovementType", page.page_id)
    if i is None:
        return
    it = i["ImprovementType"]
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    unique = p.unique_to(_col(i, "TraitType"))
    builders = p.q(
        "SELECT u.TraitType FROM Improvement_ValidBuildUnits v JOIN Units u ON u.UnitType = v.UnitType "
        "WHERE v.ImprovementType = ?",
        it,
    )
    if builders and all(r["TraitType"] for r in builders):
        for r in builders:
            unique += p.unique_to(r["TraitType"])
    unique += p.beliefs_granting("EFFECT_ADJUST_PLAYER_VALID_IMPROVEMENT", "ImprovementType", it)
    traits.lines("LOC_UI_PEDIA_UNIQUE_TO", _unique(unique))
    traits.header("LOC_UI_PEDIA_TRAITS")
    for r in p.q("SELECT * FROM Improvement_YieldChanges WHERE ImprovementType = ?", it):
        if r["YieldChange"]:
            traits.line(p.yield_line("LOC_TYPE_TRAIT_YIELD", r["YieldChange"], r["YieldType"]))
    if _col(i, "Housing"):
        traits.line("LOC_TYPE_TRAIT_HOUSING", i["Housing"] / (_col(i, "TilesRequired") or 1))
    for col, key in (
        ("AirSlots", "LOC_TYPE_TRAIT_AIRSLOTS"),
        ("CitizenSlots", "LOC_TYPE_TRAIT_CITIZENSLOTS"),
        ("WeaponSlots", "LOC_TYPE_TRAIT_WEAPONSLOTS"),
        ("Appeal", "LOC_TYPE_TRAIT_APPEAL"),
    ):
        if _col(i, col):
            traits.line(key, i[col])
    for r in p.q("SELECT * FROM Improvement_BonusYieldChanges WHERE ImprovementType = ?", it):
        prereq = p._tech_or_civic(_col(r, "PrereqCivic"), _col(r, "PrereqTech"))
        if not prereq:
            continue
        traits.line(p.yield_line("LOC_TYPE_TRAIT_BONUS_YIELD", r["BonusYieldChange"], r["YieldType"], prereq))
    for r in p.q("SELECT * FROM Improvement_Tourism WHERE ImprovementType = ?", it):
        prereq = p._tech_or_civic(_col(r, "PrereqCivic"), _col(r, "PrereqTech"))
        source = (r["TourismSource"] or "").replace("TOURISMSOURCE_", "")
        if source == "APPEAL":
            key = "LOC_TYPE_TRAIT_TOURISM_BONUS_APPEAL" if prereq else "LOC_TYPE_TRAIT_TOURISM_BONUS_APPEAL_NO_REQUIREMENT"
            traits.line(key, r["ScalingFactor"], prereq)
        else:
            key = "LOC_TYPE_TRAIT_TOURISM_BONUS_YIELD" if prereq else "LOC_TYPE_TRAIT_TOURISM_BONUS_YIELD_NO_REQUIREMENT"
            traits.line(p.yield_line(key, r["ScalingFactor"], f"YIELD_{source}", prereq))
    plunder = _col(i, "PlunderType")
    if plunder == "PLUNDER_HEAL":
        traits.line("LOC_TYPE_TRAIT_PILLAGE_AWARD_NO_ICON", "LOC_TYPE_TRAIT_PILLAGE_AWARD_HEALING")
    elif plunder and plunder != "NO_PLUNDER":
        y = p.yield_row("YIELD_" + plunder.replace("PLUNDER_", ""))
        if y is not None:
            traits.line("LOC_TYPE_TRAIT_PILLAGE_AWARD", y["IconString"], y["Name"])
    traits.lines("LOC_UI_PEDIA_ADJACENCY_BONUS", p.adjacency_lines("Improvement_Adjacencies", "ImprovementType", it))

    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    p.prereqs(req, i)
    req.lines(
        "LOC_UI_PEDIA_PLACEMENT",
        _sorted_names(
            [
                p.name("Features", "FeatureType", r["FeatureType"])
                for r in p.q("SELECT * FROM Improvement_ValidFeatures WHERE ImprovementType = ?", it)
            ]
            + [
                p.name("Resources", "ResourceType", r["ResourceType"])
                for r in p.q("SELECT * FROM Improvement_ValidResources WHERE ImprovementType = ?", it)
            ]
            + [
                p.name("Terrains", "TerrainType", r["TerrainType"])
                for r in p.q("SELECT * FROM Improvement_ValidTerrains WHERE ImprovementType = ?", it)
            ]
        ),
    )
    usage = _Box(page, p.loc, "LOC_UI_PEDIA_USAGE")
    usage.lines(
        "LOC_UI_PEDIA_BUILT_BY",
        _sorted_names(
            p.name("Units", "UnitType", r["UnitType"])
            for r in p.q("SELECT * FROM Improvement_ValidBuildUnits WHERE ImprovementType = ?", it)
        ),
    )
    p.description(page, _col(i, "Description"))


def _unlocks(p: Pedia, col: str, value: str) -> list[str]:
    names = []
    for table in ("Districts", "Buildings", "Units", "Improvements", "Projects", "Policies", "Governments"):
        name_col = "ShortName" if table == "Projects" else "Name"
        for r in p.q(f"SELECT * FROM {table} WHERE {col} = ?", value):
            if _col(r, "InternalOnly"):
                continue
            names.append(p.text(r[name_col]))
    names += [p.text(r["Name"]) for r in p.q(f"SELECT * FROM Resources WHERE {col} = ?", value)]
    names += [
        p.text(r["Name"]) for r in p.q(f"SELECT * FROM DiplomaticActions WHERE Initiator{col} = ? AND Name IS NOT NULL", value)
    ]
    if col == "PrereqCivic" and value == "CIVIC_DIPLOMATIC_SERVICE":
        names.append(p.name("Units", "UnitType", "UNIT_SPY"))  # hard-coded in the game's CivilopediaPage_Civic.lua
    return _sorted_names(names)


def technology(p: Pedia, page: Page, civic: bool = False) -> None:
    table, key_col = ("Civics", "CivicType") if civic else ("Technologies", "TechnologyType")
    t = p.get(table, key_col, page.page_id)
    if t is None:
        return
    tid = t[key_col]
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    mod_table = "CivicModifiers" if civic else "TechnologyModifiers"
    for effect, key in (("EFFECT_GRANT_SPY", "LOC_TYPE_TRAIT_SPIES"), ("EFFECT_GRANT_INFLUENCE_TOKEN", "LOC_TYPE_TRAIT_ENVOYS")):
        rows = p.q(
            f"SELECT SUM(CAST(a.Value AS REAL)) AS n FROM {mod_table} tm JOIN Modifiers m ON m.ModifierId = tm.ModifierId "
            "JOIN DynamicModifiers d ON d.ModifierType = m.ModifierType JOIN ModifierArguments a ON a.ModifierId = m.ModifierId "
            f"WHERE tm.{key_col} = ? AND d.EffectType = ? AND a.Name = 'Amount'",
            tid,
            effect,
        )
        if rows and rows[0]["n"]:
            traits.line(key, rows[0]["n"])
    traits.lines("LOC_UI_PEDIA_UNLOCKS", _unlocks(p, "PrereqCivic" if civic else "PrereqTech", tid))
    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    era = p.name("Eras", "EraType", _col(t, "EraType"))
    if era:
        req.line(era)
    prereq_table, prereq_col, own_col = (
        ("CivicPrereqs", "PrereqCivic", "Civic") if civic else ("TechnologyPrereqs", "PrereqTech", "Technology")
    )
    req.lines(
        "LOC_UI_PEDIA_REQUIRED_CIVICS" if civic else "LOC_UI_PEDIA_REQUIRED_TECHNOLOGIES",
        [p.name(table, key_col, r[prereq_col]) for r in p.q(f"SELECT * FROM {prereq_table} WHERE {own_col} = ?", tid)],
    )
    req.header("LOC_UI_PEDIA_CULTURE_COST" if civic else "LOC_UI_PEDIA_RESEARCH_COST")
    req.line(p.cost_line(t["Cost"], "YIELD_CULTURE" if civic else "YIELD_SCIENCE"))
    req.lines(
        "LOC_UI_PEDIA_BOOSTS", [p.text(r["TriggerDescription"]) for r in p.q(f"SELECT * FROM Boosts WHERE {key_col} = ?", tid)]
    )
    prog = _Box(page, p.loc, "LOC_UI_PEDIA_PROGRESSION")
    prog.lines(
        "LOC_UI_PEDIA_LEADS_TO_CIVICS" if civic else "LOC_UI_PEDIA_LEADS_TO_TECHS",
        [p.name(table, key_col, r[own_col]) for r in p.q(f"SELECT * FROM {prereq_table} WHERE {prereq_col} = ?", tid)],
    )
    p.description(page, _col(t, "Description"))


def civic(p: Pedia, page: Page) -> None:
    technology(p, page, civic=True)


def resource(p: Pedia, page: Page) -> None:
    r0 = p.get("Resources", "ResourceType", page.page_id)
    if r0 is None:
        return
    rt = r0["ResourceType"]
    cls = (_col(r0, "ResourceClassType") or "").replace("RESOURCECLASS_", "")
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    if cls in ("BONUS", "LUXURY", "STRATEGIC", "ARTIFACT"):
        traits.line(f"LOC_RESOURCECLASS_{cls}_NAME")
    for y in p.q("SELECT * FROM Yields"):
        total = sum(
            r["YieldChange"]
            for r in p.q("SELECT * FROM Resource_YieldChanges WHERE ResourceType = ? AND YieldType = ?", rt, y["YieldType"])
        )
        if total:
            traits.line("LOC_TYPE_TRAIT_YIELD", total, y["IconString"], y["Name"])
    if (_col(r0, "Happiness") or 0) > 0:
        traits.line("LOC_TYPE_TRAIT_HAPPINESS", r0["Happiness"])
    for r in p.q("SELECT * FROM Resource_Consumption WHERE ResourceType = ?", rt):
        if _col(r, "Accumulate") and (_col(r, "ImprovedExtractionRate") or 0) > 0:
            traits.line("LOC_RESOURCE_ACCUMULATION_WHEN_IMPROVED", r["ImprovedExtractionRate"], f"[ICON_{rt}]", r0["Name"])
    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    unlocked = p._tech_or_civic(_col(r0, "PrereqCivic"), _col(r0, "PrereqTech"))
    req.lines("LOC_UI_PEDIA_UNLOCKED_BY", [p.text(unlocked)] if unlocked else [])
    if rt in ("RESOURCE_CLOVES", "RESOURCE_CINNAMON"):  # hard-coded in the game's CivilopediaPage_Resource.lua
        req.lines("LOC_UI_PEDIA_SUZERAIN", [p.name("Civilizations", "CivilizationType", "CIVILIZATION_ZANZIBAR")])
    creators = []
    for gp in p.q("SELECT DISTINCT GreatPersonIndividualType FROM GreatPersonIndividualActionModifiers"):
        gpt = gp["GreatPersonIndividualType"]
        args = p.modifier_args(
            "GreatPersonIndividualActionModifiers",
            "GreatPersonIndividualType",
            gpt,
            "EFFECT_GRANT_FREE_RESOURCE_IN_CITY",
            "ResourceType",
        )
        if rt in args:
            creators.append(p.name("GreatPersonIndividuals", "GreatPersonIndividualType", gpt))
    req.lines("LOC_UI_PEDIA_CREATED_BY", creators)
    req.lines(
        "LOC_UI_PEDIA_PLACEMENT",
        _sorted_names(
            [
                p.name("Features", "FeatureType", r["FeatureType"])
                for r in p.q("SELECT * FROM Resource_ValidFeatures WHERE ResourceType = ?", rt)
            ]
            + [
                p.name("Terrains", "TerrainType", r["TerrainType"])
                for r in p.q("SELECT * FROM Resource_ValidTerrains WHERE ResourceType = ?", rt)
            ]
        ),
    )
    usage = _Box(page, p.loc, "LOC_UI_PEDIA_USAGE")
    usage.lines(
        "LOC_UI_PEDIA_IMPROVED_BY",
        _sorted_names(
            p.name("Improvements", "ImprovementType", r["ImprovementType"])
            for r in p.q("SELECT * FROM Improvement_ValidResources WHERE ResourceType = ?", rt)
        ),
    )
    harvest = []
    for r in p.q("SELECT * FROM Resource_Harvests WHERE ResourceType = ?", rt):
        line = p.yield_line("LOC_TYPE_TRAIT_YIELD", r["Amount"], r["YieldType"])
        tech = p.name("Technologies", "TechnologyType", _col(r, "PrereqTech"))
        if tech:
            line += " " + p.text("LOC_UI_PEDIA_REQUIRES", tech)
        harvest.append(line)
    usage.lines("LOC_UI_PEDIA_HARVEST", harvest)
    buildings = _sorted_names(
        p.name("Buildings", "BuildingType", r["BuildingType"])
        for r in p.q("SELECT BuildingType FROM Buildings_XP2 WHERE ResourceTypeConvertedToPower = ?", rt)
    )
    projects = _sorted_names(
        [p.name("Projects", "ProjectType", r["ProjectType"]) for r in p.q("SELECT * FROM Projects WHERE PrereqResource = ?", rt)]
        + [
            p.name("Projects", "ProjectType", r["ProjectType"])
            for r in p.q("SELECT * FROM Project_ResourceCosts WHERE ResourceType = ?", rt)
        ]
    )
    routes = _sorted_names(
        p.name("Routes", "RouteType", r["RouteType"]) for r in p.q("SELECT * FROM Route_ResourceCosts WHERE ResourceType = ?", rt)
    )
    for key, names in (
        ("LOC_UI_PEDIA_RESOURCES_BUILDINGS", buildings),
        ("LOC_UI_PEDIA_RESOURCES_PROJECTS", projects),
        ("LOC_UI_PEDIA_RESOURCES_ROUTES", routes),
    ):
        if names:
            usage.header(key, len(names))
            for n in names:
                usage.line(n)
    units = [p.text(r["Name"]) for r in p.q("SELECT Name FROM Units WHERE StrategicResource = ?", rt)]
    units += [
        p.name("Units", "UnitType", r["UnitType"])
        for r in p.q("SELECT UnitType FROM Units_XP2 WHERE ResourceMaintenanceType = ?", rt)
    ]
    units = _sorted_names(units)
    if units:
        usage.header("LOC_UI_PEDIA_RESOURCES_UNITS", len(units))
        for u in units:
            usage.line(u)


def feature(p: Pedia, page: Page, terrain: bool = False) -> None:
    table, key_col = ("Terrains", "TerrainType") if terrain else ("Features", "FeatureType")
    f = p.get(table, key_col, page.page_id)
    if f is None:
        return
    fid = f[key_col]
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    flags = []
    if terrain:
        if _col(f, "Hills"):
            flags.append(p.text("LOC_UI_PEDIA_TERRAIN_HILLS"))
        if _col(f, "Water"):
            flags.append(p.text("LOC_UI_PEDIA_TERRAIN_WATER"))
        if _col(f, "ShallowWater"):
            flags.append(p.text("LOC_UI_PEDIA_TERRAIN_SHALLOW_WATER"))
        if (_col(f, "MovementCost") or 0) > 1:
            flags.append(p.text("LOC_UI_PEDIA_MOVEMENT_CHANGE", f["MovementCost"] - 1))
    else:
        if _col(p.get("Features_XP2", "FeatureType", fid), "Volcano"):
            flags.append(p.text("LOC_UI_PEDIA_MAY_ERUPT"))
        if (_col(f, "MovementChange") or 0) > 0:
            flags.append(p.text("LOC_UI_PEDIA_MOVEMENT_CHANGE", f["MovementChange"]))
        if fid != "FEATURE_ICE" and p.q("SELECT 1 FROM RandomEvent_Yields WHERE FeatureType = ? LIMIT 1", fid):
            flags.append(p.text("LOC_UI_PEDIA_ADDITIONAL_FERTILITY"))
    if _col(f, "Impassable"):
        flags.append(p.text("LOC_UI_PEDIA_TERRAIN_IMPASSABLE"))
    if _col(f, "DefenseModifier"):
        flags.append(p.text("LOC_TOOLTIP_DEFENSE_MODIFIER", f["DefenseModifier"]))
    if _col(f, "Appeal"):
        flags.append(p.text("LOC_TYPE_TRAIT_APPEAL", f["Appeal"]))
    for line in sorted(flags):
        traits.line(line)
    yt = "Terrain_YieldChanges" if terrain else "Feature_YieldChanges"
    for y in p.q("SELECT * FROM Yields"):
        total = sum(
            r["YieldChange"] for r in p.q(f"SELECT * FROM {yt} WHERE {key_col} = ? AND YieldType = ?", fid, y["YieldType"])
        )
        if total:
            traits.line("LOC_TYPE_TRAIT_YIELD", total, y["IconString"], y["Name"])
    rt = "Resource_ValidTerrains" if terrain else "Resource_ValidFeatures"
    traits.lines(
        "LOC_UI_PEDIA_VALID_RESOURCES",
        _sorted_names(
            p.name("Resources", "ResourceType", r["ResourceType"]) for r in p.q(f"SELECT * FROM {rt} WHERE {key_col} = ?", fid)
        ),
    )
    if not terrain:
        p.description(page, _col(f, "Description"))


def terrain(p: Pedia, page: Page) -> None:
    feature(p, page, terrain=True)


def civilization(p: Pedia, page: Page) -> None:
    c = p.get("Civilizations", "CivilizationType", page.page_id)
    if c is None:
        return
    ct = c["CivilizationType"]
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    traits.lines(
        "LOC_UI_PEDIA_LEADERS",
        [
            p.name("Leaders", "LeaderType", r["LeaderType"])
            for r in p.q("SELECT * FROM CivilizationLeaders WHERE CivilizationType = ?", ct)
        ],
    )
    units, infra = p.trait_owned("CivilizationTraits", "CivilizationType", [ct])
    traits.lines("LOC_UI_PEDIA_SPECIAL_UNITS", units)
    traits.lines("LOC_UI_PEDIA_SPECIAL_INFRASTRUCTURE", infra)
    prefs = _Box(page, p.loc, "LOC_UI_PEDIA_PREFERENCES")
    prefs.lines(
        "LOC_UI_PEDIA_PREFERRED_RELIGION",
        [
            p.name("Religions", "ReligionType", r["ReligionType"])
            for r in p.q("SELECT * FROM FavoredReligions WHERE CivilizationType = ? AND LeaderType IS NULL", ct)
        ],
    )
    bias = _Box(page, p.loc, "LOC_UI_PEDIA_START_BIAS")
    lines = [
        f"{p.text('LOC_TOOLTIP_RIVER')}: {r['Tier']}" for r in p.q("SELECT * FROM StartBiasRivers WHERE CivilizationType = ?", ct)
    ]
    for table, col, names in (
        ("StartBiasTerrains", "TerrainType", "Terrains"),
        ("StartBiasFeatures", "FeatureType", "Features"),
        ("StartBiasResources", "ResourceType", "Resources"),
    ):
        lines += [
            f"{p.name(names, col, r[col])}: {r['Tier']}" for r in p.q(f"SELECT * FROM {table} WHERE CivilizationType = ?", ct)
        ]
    for line in lines:
        bias.line(line)
    geo = _Box(page, p.loc, "LOC_UI_PEDIA_GEOGRAPHY_AND_SOCIAL_DATA")
    info = p.q("SELECT * FROM CivilizationInfo WHERE CivilizationType = ?", ct)
    for r in sorted(info, key=lambda r: (float(r["SortIndex"] or 0), p.text(r["Header"]))):
        geo.header(r["Header"])
        geo.line(r["Caption"])
    p.unique_abilities(page, "CivilizationTraits", "CivilizationType", [ct])


def leader(p: Pedia, page: Page) -> None:
    lt = page.page_id
    chain = []
    current = lt
    while current and current not in chain:
        chain.append(current)
        current = _col(p.get("Leaders", "LeaderType", current), "InheritFrom")
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    traits.lines(
        "LOC_UI_PEDIA_CIVILIZATIONS",
        [
            p.name("Civilizations", "CivilizationType", r["CivilizationType"])
            for x in chain
            for r in p.q("SELECT * FROM CivilizationLeaders WHERE LeaderType = ?", x)
        ],
    )
    units, infra = p.trait_owned("LeaderTraits", "LeaderType", chain)
    traits.lines("LOC_UI_PEDIA_SPECIAL_UNITS", units)
    traits.lines("LOC_UI_PEDIA_SPECIAL_INFRASTRUCTURE", infra)
    prefs = _Box(page, p.loc, "LOC_UI_PEDIA_PREFERENCES")
    agendas = []
    for r in p.q(
        "SELECT a.Name, a.Description FROM HistoricalAgendas h JOIN Agendas a ON a.AgendaType = h.AgendaType "
        f"WHERE h.LeaderType IN ({','.join('?' * len(chain))})",
        *chain,
    ):
        agendas.append(f"{p.text(r['Name'])}: {p.text(r['Description'])}")
    prefs.lines("LOC_UI_PEDIA_AGENDAS", agendas)
    prefs.lines(
        "LOC_UI_PEDIA_PREFERRED_RELIGION",
        [
            p.name("Religions", "ReligionType", r["ReligionType"])
            for x in chain
            for r in p.q("SELECT * FROM FavoredReligions WHERE CivilizationType IS NULL AND LeaderType = ?", x)
        ],
    )
    p.unique_abilities(page, "LeaderTraits", "LeaderType", chain)


def city_state(p: Pedia, page: Page) -> None:
    ct = page.page_id
    leaders = [r["LeaderType"] for r in p.q("SELECT * FROM CivilizationLeaders WHERE CivilizationType = ?", ct)]
    inherited = [_col(p.get("Leaders", "LeaderType", lt), "InheritFrom") or "" for lt in leaders]
    box = _Box(page, p.loc, "LOC_UI_PEDIA_CITY_STATE_TYPE")
    for parent in inherited:
        kind = parent.replace("LEADER_MINOR_CIV_", "")
        key = "LOC_CITY_STATES_SCENARIO_TYPE_MARITIME" if kind == "MARITIME" else f"LOC_CITY_STATES_TYPE_{kind}"
        if p.loc.has(key):
            box.line(key)
    chain = []
    for lt in leaders:
        while lt and lt not in chain:
            chain.append(lt)
            lt = _col(p.get("Leaders", "LeaderType", lt), "InheritFrom")
    before = list(page.chapters)
    page.chapters.clear()
    p.unique_abilities(page, "CivilizationTraits", "CivilizationType", [ct], filter_used=False)
    civ_abilities = list(page.chapters)
    page.chapters.clear()
    p.unique_abilities(page, "LeaderTraits", "LeaderType", chain, filter_used=False)
    page.chapters = civ_abilities + [c for c in page.chapters if c not in civ_abilities] + before


def policy(p: Pedia, page: Page) -> None:
    pol = p.get("Policies", "PolicyType", page.page_id)
    if pol is None:
        return
    pt = pol["PolicyType"]
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    slot = p.name("GovernmentSlots", "GovernmentSlotType", _col(pol, "GovernmentSlotType"))
    if slot:
        traits.line(slot)
    traits.lines(
        "LOC_UI_PEDIA_MADE_OBSOLETE_BY",
        [
            p.name("Policies", "PolicyType", r["ObsoletePolicy"])
            for r in p.q("SELECT * FROM ObsoletePolicies WHERE PolicyType = ?", pt)
        ],
    )
    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    if _col(p.get("Policies_XP1", "PolicyType", pt), "RequiresDarkAge"):
        req.line("LOC_UI_PEDIA_REQUIRES_DARK_AGE")
    for col, key in (("MinimumGameEra", "LOC_UI_PEDIA_MIN_ERA"), ("MaximumGameEra", "LOC_UI_PEDIA_MAX_ERA")):
        era = _col(p.get("Policies_XP1", "PolicyType", pt), col)
        if era:
            req.line(key, p.name("Eras", "EraType", era))
    p.prereqs(req, pol)
    exclusive = [
        p.name("Governments", "GovernmentType", r["GovernmentType"])
        for r in p.q("SELECT * FROM Policy_GovernmentExclusives_XP2 WHERE PolicyType = ?", pt)
    ]
    req.lines("LOC_GOVERNMENT_NAME", exclusive)
    unlocked_by = [p.text(r["Name"]) for r in p.q("SELECT Name FROM Governments WHERE PolicyToUnlock = ?", pt)]
    if unlocked_by:
        if not exclusive:
            req.header("LOC_GOVERNMENT_NAME")
        req.line(f"{p.text('LOC_UI_PEDIA_REQUIRES_NOT_GOVERNMENT')} {', '.join(unlocked_by)}")
    p.description(page, _col(pol, "Description"))


def government(p: Pedia, page: Page) -> None:
    g = p.get("Governments", "GovernmentType", page.page_id)
    if g is None:
        return
    gt = g["GovernmentType"]
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    favor = _col(p.get("Governments_XP2", "GovernmentType", gt), "Favor")
    if favor:
        traits.line("LOC_UI_PEDIA_TRAIT_FAVOR", favor)
    if _col(g, "OtherGovernmentIntolerance"):
        traits.line("LOC_UI_PEDIA_TRAIT_INTOLERANCE", g["OtherGovernmentIntolerance"])
    for r in p.q("SELECT * FROM Government_SlotCounts WHERE GovernmentType = ?", gt):
        slot = _col(p.get("GovernmentSlots", "GovernmentSlotType", r["GovernmentSlotType"]), "Name")
        traits.line("LOC_UI_PEDIA_TRAIT_SLOT_TYPE", r["NumSlots"], slot)
    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    p.prereqs(req, g)
    req.lines(
        "LOC_UI_PEDIA_MADE_EXCLUSIVE_GOVENMENT",
        [
            p.name("Policies", "PolicyType", r["PolicyType"])
            for r in p.q("SELECT * FROM Policy_GovernmentExclusives_XP2 WHERE GovernmentType = ?", gt)
        ],
    )
    influence = []
    if _col(g, "InfluencePointsPerTurn"):
        influence.append(
            p.text(
                "LOC_GOVT_INFLUENCE_POINTS_TOWARDS_ENVOYS",
                g["InfluencePointsPerTurn"],
                _col(g, "InfluencePointsThreshold"),
                _col(g, "InfluenceTokensPerThreshold"),
            )
        )
    if favor:
        influence.append(p.text("LOC_GOVT_FAVOR_PER_TURN", favor))
    traits.lines("LOC_HUD_REPORTS_BONUSES", influence)
    inherent, accumulated = _col(g, "InherentBonusDesc"), _col(g, "AccumulatedBonusShortDesc")
    if inherent and accumulated:
        page.chapters[0:0] = [
            Chapter(p.text("LOC_GOVERNMENT_INHERENT_BONUS"), [p.text(inherent)]),
            Chapter(p.text("LOC_GOVERNMENT_ACCUMULATED_BONUS"), [p.text(accumulated)]),
        ]


def project(p: Pedia, page: Page) -> None:
    pr = p.get("Projects", "ProjectType", page.page_id)
    if pr is None:
        return
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    traits.lines(
        "LOC_UI_PEDIA_UNIQUE_TO", p.unique_to(_col(p.get("Districts", "DistrictType", _col(pr, "PrereqDistrict")), "TraitType"))
    )
    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    p.prereqs(req, pr)
    req.lines("LOC_RESOURCE_NAME", [p.name("Resources", "ResourceType", _col(pr, "PrereqResource"))])
    req.lines(
        "LOC_UI_PEDIA_PROJECTS",
        _sorted_names(
            p.name("Projects", "ProjectType", r["PrereqProjectType"], "ShortName")
            for r in p.q("SELECT * FROM ProjectPrereqs WHERE ProjectType = ?", pr["ProjectType"])
        ),
    )
    if _col(pr, "Cost") and (_col(pr, "CostProgressionModel") or "NO_PROGRESSION_MODEL") == "NO_PROGRESSION_MODEL":
        req.header("LOC_UI_PEDIA_PRODUCTION_COST")
        req.line(p.cost_line(pr["Cost"], "YIELD_PRODUCTION"))
    if "CREATE_HERO" in pr["ProjectType"]:
        hero = pr["ProjectType"].replace("PROJECT_CREATE_HERO_", "")
        unlocks = _Box(page, p.loc, "LOC_UI_PEDIA_UNLOCKS")
        unlocks.line(f"LOC_UNIT_HERO_{hero}_NAME")
    p.description(page, _col(pr, "Description"))


def great_person(p: Pedia, page: Page) -> None:
    gp = p.get("GreatPersonIndividuals", "GreatPersonIndividualType", page.page_id)
    if gp is None:
        return
    gpt = gp["GreatPersonIndividualType"]
    gp_class = p.get("GreatPersonClasses", "GreatPersonClassType", _col(gp, "GreatPersonClassType"))
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    era = p.name("Eras", "EraType", _col(gp, "EraType"))
    if era and _col(gp_class, "AvailableInTimeline"):
        traits.line(era)
    if gp_class is not None:
        traits.line(p.text(gp_class["Name"]))
    usage = _Box(page, p.loc, "LOC_UI_PEDIA_USAGE")
    usage.lines(
        "LOC_UI_PEDIA_CREATES",
        [
            p.name("Resources", "ResourceType", r)
            for r in p.modifier_args(
                "GreatPersonIndividualActionModifiers",
                "GreatPersonIndividualType",
                gpt,
                "EFFECT_GRANT_FREE_RESOURCE_IN_CITY",
                "ResourceType",
            )
        ],
    )

    def abilities(table: str) -> list[str]:
        rows = p.q(f"SELECT ModifierId FROM {table} WHERE GreatPersonIndividualType = ?", gpt)
        return [t for t in (p.modifier_text(r["ModifierId"]) for r in rows) if t]

    active, passive = abilities("GreatPersonIndividualActionModifiers"), abilities("GreatPersonIndividualBirthModifiers")
    chapters = []
    works = [p.text(r["Name"]) for r in p.q("SELECT Name FROM GreatWorks WHERE GreatPersonIndividualType = ?", gpt)]
    if works:
        paragraphs = ["\n".join("• " + w for w in works)]
        if p.loc.has("LOC_GREATPERSON_ACTION_USAGE_CREATE_GREAT_WORK"):
            paragraphs.append(p.text("LOC_GREATPERSON_ACTION_USAGE_CREATE_GREAT_WORK"))
        chapters.append(Chapter(p.text("LOC_UI_PEDIA_GREAT_WORKS"), paragraphs))
    heading = p.text("LOC_UI_PEDIA_UNIQUE_ABILITY")
    if _col(gp, "GreatPersonClassType") == "GREAT_PERSON_CLASS_PROPHET" and p.loc.has(
        "LOC_GREATPERSON_ACTION_USAGE_FOUND_RELIGION"
    ):
        chapters.append(Chapter(heading, [p.text("LOC_GREATPERSON_ACTION_USAGE_FOUND_RELIGION")]))
    if (_col(gp, "ActionCharges") or 0) > 0 and (active or _col(gp, "ActionEffectTextOverride")):
        title = p.text(
            "LOC_UI_PEDIA_GREATPERSON_ACTION",
            _col(gp, "ActionNameTextOverride") or "LOC_GREATPERSON_ACTION_NAME_DEFAULT",
            gp["ActionCharges"],
        )
        body = p.text(gp["ActionEffectTextOverride"]) if _col(gp, "ActionEffectTextOverride") else "\n".join(active)
        chapters.append(Chapter(f"{heading}: {title}", [body]))
    if passive or _col(gp, "BirthEffectTextOverride"):
        title = p.text(_col(gp, "BirthNameTextOverride") or "LOC_GREATPERSON_PASSIVE_NAME_DEFAULT")
        body = p.text(gp["BirthEffectTextOverride"]) if _col(gp, "BirthEffectTextOverride") else "\n".join(passive)
        chapters.append(Chapter(f"{heading}: {title}", [body]))
    page.chapters[0:0] = chapters


def belief(p: Pedia, page: Page) -> None:
    b = p.get("Beliefs", "BeliefType", page.page_id)
    if b is None:
        return
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_BELIEF_CLASS")
    cls = p.name("BeliefClasses", "BeliefClassType", _col(b, "BeliefClassType"))
    if cls:
        traits.line(cls)
    bt = b["BeliefType"]

    def unlocked(effect, arg, table, col):
        return [p.name(table, col, v) for v in p.modifier_args("BeliefModifiers", "BeliefType", bt, effect, arg)]

    traits.lines(
        "LOC_UI_PEDIA_SPECIAL_INFRASTRUCTURE",
        unlocked("EFFECT_ADD_RELIGIOUS_BUILDING", "BuildingType", "Buildings", "BuildingType")
        + unlocked("EFFECT_ADJUST_PLAYER_VALID_IMPROVEMENT", "ImprovementType", "Improvements", "ImprovementType"),
    )
    traits.lines("LOC_UI_PEDIA_SPECIAL_UNITS", unlocked("EFFECT_ADD_RELIGIOUS_UNIT", "UnitType", "Units", "UnitType"))
    p.description(page, _col(b, "Description"))


def religion(p: Pedia, page: Page) -> None:
    followers = []
    for r in p.q("SELECT * FROM FavoredReligions WHERE ReligionType = ?", page.page_id):
        if not _col(r, "LeaderType") and _col(r, "CivilizationType"):
            followers.append(p.name("Civilizations", "CivilizationType", r["CivilizationType"]))
        elif _col(r, "LeaderType"):
            followers.append(p.name("Leaders", "LeaderType", r["LeaderType"]))
    box = _Box(page, p.loc, "LOC_UI_PEDIA_FOLLOWERS")
    for f in followers:
        box.line(f)


def route(p: Pedia, page: Page) -> None:
    r = p.get("Routes", "RouteType", page.page_id)
    if r is None:
        return
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    if _col(r, "MovementCost") is not None:
        traits.line("LOC_ROUTE_MOVEMENT_COST", r["MovementCost"])
    if _col(r, "SupportsBridges"):
        traits.line("LOC_ROUTE_SUPPORTS_BRIDGES")
    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    req.lines("LOC_ERA_NAME", [p.name("Eras", "EraType", _col(r, "PrereqEra"))])
    xp2 = p.get("Routes_XP2", "RouteType", r["RouteType"])
    req.lines("LOC_TECHNOLOGY_NAME", [p.name("Technologies", "TechnologyType", _col(xp2, "PrereqTech"))])
    builders = [
        p.name("Units", "UnitType", b["UnitType"])
        for b in p.q("SELECT * FROM Route_ValidBuildUnits WHERE RouteType = ?", r["RouteType"])
    ]
    if not _col(xp2, "BuildOnlyWithUnit"):
        builders.append(p.name("Units", "UnitType", "UNIT_TRADER"))
    usage = _Box(page, p.loc, "LOC_UI_PEDIA_USAGE")
    usage.lines("LOC_UI_PEDIA_BUILT_BY", _sorted_names(builders))
    p.description(page, _col(r, "Description"))


def unit_promotion(p: Pedia, page: Page) -> None:
    up = p.get("UnitPromotions", "UnitPromotionType", page.page_id)
    if up is None:
        return
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_PROMOTION_CLASS")
    traits.line(p.name("UnitPromotionClasses", "PromotionClassType", _col(up, "PromotionClass")))
    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    req.lines(
        "LOC_UI_PEDIA_PROMOTIONS",
        _sorted_names(
            p.name("UnitPromotions", "UnitPromotionType", r["PrereqUnitPromotion"])
            for r in p.q("SELECT * FROM UnitPromotionPrereqs WHERE UnitPromotion = ?", up["UnitPromotionType"])
        ),
    )
    p.description(page, _col(up, "Description"))


def governor(p: Pedia, page: Page) -> None:
    g = p.get("Governors", "GovernorType", page.page_id)
    if g is None:
        return
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TITLES")
    for name in _sorted_names(
        p.name("GovernorPromotions", "GovernorPromotionType", r["GovernorPromotion"])
        for r in p.q("SELECT * FROM GovernorPromotionSets WHERE GovernorType = ?", g["GovernorType"])
    ):
        traits.line(name)
    p.description(page, _col(g, "Description"))


def governor_promotion(p: Pedia, page: Page) -> None:
    gp = p.get("GovernorPromotions", "GovernorPromotionType", page.page_id)
    if gp is None:
        return
    gpt = gp["GovernorPromotionType"]
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    level = _col(gp, "Level") or 0
    traits.line("LOC_UI_PEDIA_PROMOTION_BASE" if level < 1 else "LOC_UI_PEDIA_PROMOTION_LEVEL", level)
    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    for r in p.q("SELECT * FROM GovernorPromotionPrereqs WHERE GovernorPromotionType = ?", gpt):
        req.line(p.name("GovernorPromotions", "GovernorPromotionType", r["PrereqGovernorPromotion"]))
    prog = _Box(page, p.loc, "LOC_UI_PEDIA_PROGRESSION")
    prog.lines(
        "LOC_UI_PEDIA_LEADS_TO_GOVERNOR_PROMOTIONS",
        [
            p.name("GovernorPromotions", "GovernorPromotionType", r["GovernorPromotionType"])
            for r in p.q("SELECT * FROM GovernorPromotionPrereqs WHERE PrereqGovernorPromotion = ?", gpt)
        ],
    )
    p.description(page, _col(gp, "Description"))


def historic_moment(p: Pedia, page: Page) -> None:
    m = p.get("Moments", "MomentType", page.page_id)
    if m is None:
        return
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    traits.line("LOC_UI_PEDIA_ERA_SCORE", _col(m, "EraScore") or 0)
    obsolete = p.name("Eras", "EraType", _col(m, "ObsoleteEra"))
    traits.lines("LOC_UI_PEDIA_MADE_REMOVED_IF", [obsolete])
    req = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    for col, key in (("MinimumGameEra", "LOC_UI_PEDIA_MIN_ERA"), ("MaximumGameEra", "LOC_UI_PEDIA_MAX_ERA")):
        era = p.name("Eras", "EraType", _col(m, col))
        if era:
            req.line(key, era)
    p.description(page, _col(m, "Description"))


# -- Better Civilopedia layouts ---------------------------------------------------------------
# The Better Civilopedia mod (workshop 1341630847) adds these page types; ports of its
# CivilopediaPage_BCP_Base/XP1/XP2.lua and of the extra facts its CivilopediaScreen_BCP.lua adds.

_UNIT_TABLE_STATS = (
    ("BaseMoves", "LOC_UI_PEDIA_MOVEMENT_POINTS"),
    ("Combat", "LOC_UI_PEDIA_MELEE_STRENGTH"),
    ("RangedCombat", "LOC_UI_PEDIA_RANGED_STRENGTH"),
    ("Range", "LOC_UI_PEDIA_RANGE"),
    ("Bombard", "LOC_UI_PEDIA_BOMBARD_STRENGTH"),
    ("Cost", "LOC_HUD_PRODUCTION_COST"),
)


def _unit_group(base) -> str:
    formation, promo = _col(base, "FormationClass"), _col(base, "PromotionClass")
    if formation == "FORMATION_CLASS_AIR":
        return "AIR"
    if formation == "FORMATION_CLASS_NAVAL":
        return "NAVAL"
    if formation == "FORMATION_CLASS_SUPPORT":
        return "SUPPORT"
    if formation == "FORMATION_CLASS_LAND_COMBAT":
        if promo in ("PROMOTION_CLASS_HEAVY_CAVALRY", "PROMOTION_CLASS_LIGHT_CAVALRY"):
            return "CAVALRY"
        if promo in ("PROMOTION_CLASS_RANGED", "PROMOTION_CLASS_SIEGE"):
            return "RANGED"
        return "MELEE"
    return ""


def table_units(p: Pedia, page: Page) -> None:
    """Every unit of one kind (melee, naval, ...) by era, with its main stats."""
    replaces = {r["CivUniqueUnitType"]: r["ReplacesUnitType"] for r in p.q("SELECT * FROM UnitReplaces")}
    heroes = {r["UnitType"] for r in p.q("SELECT UnitType FROM HeroClasses")}
    eras = {r["EraType"]: r for r in p.q("SELECT * FROM Eras")}
    rows = []
    for u in p.q("SELECT * FROM Units"):
        if _col(u, "TraitType") == "TRAIT_BARBARIAN" or u["UnitType"] in heroes:
            continue
        base = p.get("Units", "UnitType", replaces.get(u["UnitType"])) or u
        if _unit_group(base) != page.page_id:
            continue
        prereq = p.get("Technologies", "TechnologyType", _col(base, "PrereqTech")) or p.get(
            "Civics", "CivicType", _col(base, "PrereqCivic")
        )
        era = eras.get(_col(prereq, "EraType")) or next(iter(sorted(eras.values(), key=_era_order)), None)
        unique = bool(_col(u, "TraitType"))
        rows.append((_era_order(era), base["Cost"] or 0, base["UnitType"], unique, u["UnitType"], u, base, era))
    rows.sort(key=lambda r: r[:5])
    group = None
    for _, _, _, unique, _, u, base, era in rows:
        heading = p.text(_col(era, "Name"))
        if group is None or group.heading != heading:
            group = FactGroup(heading)
            page.facts.append(group)
        stats = [f"{p.text(key)} {_num(u[col])}" for col, key in _UNIT_TABLE_STATS if _col(u, col)]
        notes = p.unique_to(_col(u, "TraitType")) if unique else []
        if base is not u:
            notes.append(f"{p.text('LOC_UI_PEDIA_REPLACES')} {p.text(base['Name'])}")
        name = p.text(u["Name"]) + (f" ({', '.join(notes)})" if notes else "")
        group.lines.append(f"{name}: {', '.join(stats)}" if stats else name)


def _era_order(era) -> int:
    return _col(era, "ChronologyIndex") or 0


def adjacencies(p: Pedia, page: Page) -> None:
    """The adjacency bonuses of every district: standard ones first, then the unique ones."""
    districts = [
        d for d in p.q("SELECT * FROM Districts") if d["DistrictType"] not in ("DISTRICT_CITY_CENTER", "DISTRICT_WONDER")
    ]
    districts.sort(key=lambda d: (bool(_col(d, "TraitType")), p.text(d["Name"]).lower()))
    for d in districts:
        lines = p.adjacency_lines("District_Adjacencies", "DistrictType", d["DistrictType"])
        if lines:
            unique = p.unique_to(_col(d, "TraitType"))
            name = p.text(d["Name"]) + (f" ({', '.join(unique)})" if unique else "")
            page.facts.append(FactGroup(name, lines))


_TIME_STRATEGIES = [
    "ANCIENT",
    "CLASSICAL",
    "MEDIEVAL",
    "RENAISSANCE",
    "INDUSTRIAL",
    "MODERN",
    "ATOMIC",
    "INFORMATION",
    "FUTURE",
]


def time_strategy(p: Pedia, page: Page) -> None:
    """What the AI favours in one era (its AiFavoredItems for that era's strategy), and the total so far."""
    era = page.page_id.replace("TIME_", "").replace("_CHANGES", "")
    if era not in _TIME_STRATEGIES:
        return
    upto = _TIME_STRATEGIES.index(era)
    values: dict[str, list] = {}
    for i, name in enumerate(_TIME_STRATEGIES[: upto + 1]):
        for r in p.q(
            "SELECT f.Item, f.Value FROM Strategy_Priorities s JOIN AiFavoredItems f ON f.ListType = s.ListType "
            "WHERE s.StrategyType = ?",
            f"STRATEGY_{name}_CHANGES",
        ):
            values.setdefault(r["Item"], [None] * (upto + 1))[i] = r["Value"] or 0
    lines = []
    for item in sorted(values, key=lambda i: p.type_name(i).lower()):
        now = values[item][upto]
        if now is None:
            continue
        total = sum(v for v in values[item] if v)
        lines.append(f"{p.type_name(item)}: {_signed(now)} ({p.text('LOC_HUD_CITY_TOTAL')} {_signed(total)})")
    if lines:
        page.facts.append(FactGroup(f"{p.text('LOC_BCP_AI')} {p.text('LOC_HUD_REPORTS_PRIORITY')}", lines))


def _signed(v) -> str:
    return f"+{_num(v)}" if v > 0 else _num(v)


def rand_agenda(p: Pedia, page: Page) -> None:
    a = p.get("Agendas", "AgendaType", page.page_id)
    if a is None:
        return
    at = a["AgendaType"]
    traits = _Box(page, p.loc, "LOC_UI_PEDIA_TRAITS")
    limit = _col(p.get("RandomAgendas", "AgendaType", at), "GameLimit") or 0
    if limit > 0:
        traits.line(f"{p.text('LOC_VISIBILITY_LIMITED_NAME')} {_num(limit)}")
    if _col(p.get("RandomAgendas_XP2", "AgendaType", at), "RequiresReligion"):
        traits.line("LOC_UI_RELIGION_TITLE")
    traits.lines(
        "LOC_UI_PEDIA_LEADERS",
        [
            f"{p.name('Leaders', 'LeaderType', r['LeaderType'])} {_num(r['PercentageChance'])}%"
            for r in p.q("SELECT * FROM AgendaPreferredLeaders WHERE AgendaType = ?", at)
        ],
    )
    p.description(page, _col(a, "Description"))


def unit_ability(p: Pedia, page: Page) -> None:
    ab = p.get("UnitAbilities", "UnitAbilityType", page.page_id)
    if ab is None:
        return
    at = ab["UnitAbilityType"]
    usage = _Box(page, p.loc, "LOC_UI_PEDIA_USAGE")
    sources = (
        ("PolicyModifiers", "PolicyType", "Policies", "Name", "LOC_POLICY_NAME"),
        ("BuildingModifiers", "BuildingType", "Buildings", "Name", "LOC_BUILDING_NAME"),
        ("BeliefModifiers", "BeliefType", "Beliefs", "Name", "LOC_BELIEF_NAME"),
        ("TraitModifiers", "TraitType", "Traits", "Name", "LOC_UI_PEDIA_TRAITS"),
        ("GovernmentModifiers", "GovernmentType", "Governments", "Name", "LOC_GOVERNMENT_NAME"),
        (
            "CommemorationModifiers",
            "CommemorationType",
            "CommemorationTypes",
            "CategoryDescription",
            "LOC_PEDIA_CONCEPTS_PAGE_DEDICATIONS_CHAPTER_CONTENT_TITLE",
        ),
    )
    granted = []
    for r in p.q("SELECT ModifierId FROM ModifierArguments WHERE Name = 'AbilityType' AND Value = ?", at):
        for link, col, table, name_col, label in sources:
            owners = [x[0] for x in p.q(f"SELECT {col} FROM {link} WHERE ModifierId = ?", r["ModifierId"])]
            names = [p.name(table, col, o, name_col) for o in owners]
            if link == "TraitModifiers":
                # A civilization's or leader's ability: name its owner, as the player knows it.
                names = [", ".join(p.unique_to(o)) or n for o, n in zip(owners, names, strict=True)]
            names = [n for n in names if n and not n.startswith("LOC_")]
            if names:
                granted += [f"{n} ({p.text(label)})" for n in names]
                break
    usage.lines("LOC_UI_PEDIA_USAGE", _unique(granted))
    units = []
    for r in p.q("SELECT Tag FROM TypeTags WHERE Type = ?", at):
        units += [
            p.text(u["Name"])
            for u in p.q("SELECT u.Name FROM TypeTags t JOIN Units u ON u.UnitType = t.Type WHERE t.Tag = ?", r["Tag"])
        ]
    usage.lines("LOC_PEDIA_UNITS_TITLE", _sorted_names(units))
    p.description(page, _col(ab, "Description"))


def _era_span(p: Pedia, earliest, latest) -> str:
    if not earliest and not latest:
        return ""
    return f"{p.name('Eras', 'EraType', earliest)} - {p.name('Eras', 'EraType', latest)}".strip(" -")


def commemoration(p: Pedia, page: Page) -> None:
    c = p.get("CommemorationTypes", "CommemorationType", page.page_id)
    if c is None:
        return
    box = _Box(page, p.loc, "LOC_UI_PEDIA_REQUIREMENTS")
    box.lines("LOC_UI_PEDIA_APPLIES_TO", [_era_span(p, _col(c, "MinimumGameEra"), _col(c, "MaximumGameEra"))])
    bonuses = [
        p.text(_col(c, col)) for col in ("GoldenAgeBonusDescription", "NormalAgeBonusDescription", "DarkAgeBonusDescription")
    ]
    bonuses = [b for b in bonuses if b]
    if bonuses:
        page.chapters.insert(0, Chapter(p.text("LOC_HUD_REPORTS_BONUSES"), bonuses))


def alliance(p: Pedia, page: Page) -> None:
    a = p.get("Alliances", "AllianceType", page.page_id)
    if a is None:
        return
    for r in p.q(
        "SELECT e.LevelRequirement, s.Text FROM AllianceEffects e LEFT JOIN ModifierStrings s "
        "ON s.ModifierId = e.ModifierID AND s.Context = 'Summary' WHERE e.AllianceType = ? "
        "ORDER BY e.LevelRequirement, e.ModifierID",
        a["AllianceType"],
    ):
        heading = p.text("LOC_DIPLOACTION_ALLIANCE_LEVEL", r["LevelRequirement"])
        if not page.facts or page.facts[-1].heading != heading:
            page.facts.append(FactGroup(heading))
        text = p.text(r["Text"])
        if text:
            page.facts[-1].lines.append(text)


def resolution(p: Pedia, page: Page) -> None:
    r = p.get("Resolutions", "ResolutionType", page.page_id)
    if r is None:
        return
    box = _Box(page, p.loc, "LOC_OPTIONS")
    # The World Congress itself labels the two choices with the literals "A" and "B".
    for letter, col in (("A", "Effect1Description"), ("B", "Effect2Description")):
        text = p.text(_col(r, col))
        if text:
            box.line(f"{letter}: {text}")
    box.lines("LOC_UI_PEDIA_APPLIES_TO", [_era_span(p, _col(r, "EarliestEra"), _col(r, "LatestEra"))])


# Placeholders the game fills in for a live emergency; a Civilopedia page has no live one.
_EMERGENCY_WORDS = {
    "{TargetName}": "the Target",
    "{OldCityName}": "the city",
    "{1_CityName}": "the city",
    "{Turns}": "the given number of",
}


def discussion(p: Pedia, page: Page) -> None:
    d = p.get("Discussions", "DiscussionType", page.page_id)
    if d is None:
        return
    et = _col(d, "EmergencyType")
    e = p.get("EmergencyAlliances", "EmergencyType", et)

    def text(key) -> str:
        raw = p.loc.tags.get(key) if isinstance(key, str) else None
        if raw is None:
            return p.text(key)
        for k, v in _EMERGENCY_WORDS.items():
            raw = raw.replace(k, v)
        return re.sub(r"(^|[.!?]\s+)the ", lambda m: m.group(1) + "The ", p.text(raw))

    if e is not None:
        texts = p.get("EmergencyTexts", "Type", _col(e, "EmergencyText"))
        goals = p.get("EmergencyGoalTexts", "GoalType", _col(e, "GoalText"))
        box = _Box(page, p.loc, "LOC_TUTORIAL_GOAL_GENERAL_1")
        for key in (_col(goals, "GoalDescription"), _col(goals, "ListGoal")):
            box.line(text(key))
        box.lines("LOC_BCP_TARGET_GOALS", [text(_col(goals, "TargetListGoal"))])
        box.lines(
            "LOC_HUD_REPORTS_BONUSES",
            [text(r["Description"]) for r in p.q("SELECT * FROM EmergencyBuffs WHERE EmergencyType = ?", et)],
        )
        box.lines(
            "LOC_BCP_SCORE_SOURCES",
            [text(r["Description"]) for r in p.q("SELECT * FROM EmergencyScoreSources WHERE EmergencyType = ?", et)],
        )
        rewards = []
        for r in p.q("SELECT * FROM EmergencyRewards WHERE EmergencyType = ?", et):
            tier = "1. " if _col(r, "FirstPlace") else "2. " if _col(r, "TopTier") else "3. " if _col(r, "BottomTier") else ""
            reward = text(r["Description"])
            if reward:
                rewards.append(tier + reward)
        box.lines("LOC_BCP_REWARDS", rewards)
        details = [t for t in (text(_col(texts, "Description")), text(_col(texts, "ExtraEffects"))) if t]
        if details:
            page.chapters.insert(0, Chapter(p.text(_col(e, "Name")), details))
    for g in page.facts:
        g.heading = g.heading.rstrip(":")
    p.description(page, _col(d, "Description"))


def overview_moments(p: Pedia, page: Page) -> None:
    """Every historic moment, grouped by the era score it gives, biggest first."""
    by_score: dict[int, list[str]] = {}
    for m in p.q("SELECT * FROM Moments"):
        name = p.text(m["Name"])
        if not name or name.startswith("LOC_"):
            continue
        line = f"{name}: {p.text(m['Description'])}" if _col(m, "Description") else name
        span = _era_span(p, _col(m, "MinimumGameEra"), _col(m, "ObsoleteEra") or _col(m, "MaximumGameEra"))
        if span:
            line += f" ({span})"
        by_score.setdefault(_col(m, "EraScore") or 0, []).append(line)
    for score in sorted(by_score, reverse=True):
        page.facts.append(FactGroup(p.text("LOC_UI_PEDIA_ERA_SCORE", score), sorted(by_score[score])))


def _simple_description(table: str, key_col: str) -> Callable[[Pedia, Page], None]:
    def build(p: Pedia, page: Page) -> None:
        row = p.get(table, key_col, page.page_id)
        if row is not None:
            p.description(page, _col(row, "Description"))

    return build


def _num(v) -> str:
    return str(int(v)) if float(v) == int(v) else f"{v:g}"


LAYOUTS: dict[str, Callable[[Pedia, Page], None]] = {
    "District": district,
    "Building": building,
    "Unit": unit,
    "HeroUnit": unit,
    "Improvement": improvement,
    "Technology": technology,
    "Civic": civic,
    "Resource": resource,
    "Feature": feature,
    "Terrain": terrain,
    "Civilization": civilization,
    "Leader": leader,
    "CityState": city_state,
    "Policy": policy,
    "Government": government,
    "Project": project,
    "GreatPerson": great_person,
    "Belief": belief,
    "Route": route,
    "UnitPromotion": unit_promotion,
    "Governor": governor,
    "GovernorPromotion": governor_promotion,
    "HistoricMoment": historic_moment,
    "UnitAbility": unit_ability,
    "RandAgenda": rand_agenda,
    "Alliance": alliance,
    "Resolution": resolution,
    "Discussion": discussion,
    "Commemoration": commemoration,
    "TableUnits": table_units,
    "Adjacencies": adjacencies,
    "TimeStrategy": time_strategy,
    "OverviewMoments": overview_moments,
    "Religion": religion,
}
