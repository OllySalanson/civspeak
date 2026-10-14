"""A hand-made mini game install for tests.

Every name and sentence here is invented for civspeak's tests (a "Thinkery" district, a "Moon
Garden" wonder and so on). Nothing is copied from the real game; only the database shape and
the text-key naming patterns mirror it, because those are what the code reads.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest

from civspeak.paths import GamePaths

BASE_TEXT = """<?xml version="1.0" encoding="utf-8"?>
<GameData>
  <BaseGameText>
    <Row Tag="LOC_SECTION_PLACES"><Text>Districts</Text></Row>
    <Row Tag="LOC_SECTION_IDEAS"><Text>Ideas</Text></Row>
    <Row Tag="LOC_SECTION_MOMENTS"><Text>Moments</Text></Row>
    <Row Tag="LOC_DISTRICT_THINKERY_NAME"><Text>Thinkery</Text></Row>
    <Row Tag="LOC_DISTRICT_THINKERY_DESCRIPTION"><Text>A made-up district where test citizens ponder.</Text></Row>
    <Row Tag="LOC_DISTRICT_WATERWORKS_NAME"><Text>Waterworks</Text></Row>
    <Row Tag="LOC_DISTRICT_WATERWORKS_DESCRIPTION"><Text>A pretend district built beside the sea.</Text></Row>
    <Row Tag="LOC_DISTRICT_HANZEL_NAME"><Text>Hanzel</Text></Row>
    <Row Tag="LOC_DISTRICT_HANZEL_DESCRIPTION"><Text>An imaginary trading quarter.</Text></Row>
    <Row Tag="LOC_DISTRICT_MBOLO_NAME"><Text>Mbolo</Text></Row>
    <Row Tag="LOC_DISTRICT_MBOLO_DESCRIPTION"><Text>A pretend riverside market.</Text></Row>
    <Row Tag="LOC_DISTRICT_EMBOLDEN_NAME"><Text>Ambrel</Text></Row>
    <Row Tag="LOC_DISTRICT_EMBOLDEN_DESCRIPTION"><Text>A made-up hall of courage.</Text></Row>
    <Row Tag="LOC_BUILDING_MOON_GARDEN_NAME"><Text>Moon Garden</Text></Row>
    <Row Tag="LOC_BUILDING_MOON_GARDEN_DESCRIPTION"><Text>Old description that a mod replaces.</Text></Row>
    <Row Tag="LOC_MOMENT_FIRST_THINKERY_NAME"><Text>First Thinkery Finished</Text></Row>
    <Row Tag="LOC_TERRAIN_MEADOW_NAME"><Text>Meadow</Text></Row>
    <Row Tag="LOC_TERRAIN_PEAK_NAME"><Text>Peak</Text></Row>
    <Row Tag="LOC_TECH_SCRIBBLING_NAME"><Text>Scribbling</Text></Row>
    <Row Tag="LOC_YIELD_SCIENCE_NAME"><Text>Science</Text></Row>
    <Row Tag="LOC_YIELD_PRODUCTION_NAME"><Text>Production</Text></Row>
    <Row Tag="LOC_YIELD_GOLD_NAME"><Text>Gold</Text></Row>
    <Row Tag="LOC_PAGE_IDEA_DISTRICTS_TITLE"><Text>Districts</Text></Row>
    <Row Tag="LOC_PEDIA_CONCEPTS_PAGE_DISTRICTS_CHAPTER_CONTENT_PARA_1"><Text>Test districts sit within 2 tiles of a test city.</Text></Row>
    <Row Tag="LOC_PEDIA_PLACES_PAGE_DISTRICT_THINKERY_CHAPTER_HISTORY_PARA_1"><Text>Long ago, pretend scholars met here.</Text></Row>
    <Row Tag="LOC_PEDIA_PLACES_PAGE_DISTRICT_THINKERY_CHAPTER_HISTORY_PARA_2"><Text>They argued[NEWLINE]a lot.</Text></Row>
    <Row Tag="LOC_SEARCH_TERM_BRAINS"><Text>Brain Barn</Text></Row>
    <Row Tag="LOC_UI_PEDIA_DESCRIPTION"><Text>Description</Text></Row>
    <Row Tag="LOC_UI_PEDIA_HISTORICAL_CONTEXT"><Text>Historical Context</Text></Row>
    <Row Tag="LOC_UI_PEDIA_TRAITS"><Text>Traits</Text></Row>
    <Row Tag="LOC_UI_PEDIA_REQUIREMENTS"><Text>Requirements</Text></Row>
    <Row Tag="LOC_UI_PEDIA_USAGE"><Text>Usage</Text></Row>
    <Row Tag="LOC_UI_PEDIA_PLACEMENT"><Text>Placement</Text></Row>
    <Row Tag="LOC_UI_PEDIA_PLACEMENT_ADJ_TO_COAST"><Text>Must be next to the coast</Text></Row>
    <Row Tag="LOC_UI_PEDIA_ADJACENCY_BONUS"><Text>Adjacency Bonus</Text></Row>
    <Row Tag="LOC_UI_PEDIA_PRODUCTION_COST"><Text>Production Cost</Text></Row>
    <Row Tag="LOC_UI_PEDIA_BASE_COST"><Text>Base Cost: {1_Amount : number #} {2_Icon} {3_Name}</Text></Row>
    <Row Tag="LOC_UI_PEDIA_USAGE_UNLOCKS_BUILDINGS"><Text>Unlocks Buildings</Text></Row>
    <Row Tag="LOC_TECHNOLOGY_NAME"><Text>Technology</Text></Row>
    <Row Tag="LOC_TYPE_TRAIT_ADJACENT_BONUS"><Text>{1_Amount: number +#,###.#;-#,###.#} {2_YieldIcon} {3_YieldName} from each adjacent {5_AdjacentObject} tile.</Text></Row>
    <Row Tag="LOC_TYPE_TRAIT_ADJACENT_BONUS_PER"><Text>{1_Amount: number +#,###.#;-#,###.#} {2_YieldIcon} {3_YieldName} from every {4_Count} adjacent {5_AdjacentObject} tiles.</Text></Row>
    <Row Tag="LOC_TYPE_TRAIT_ADJACENT_OBJECT_DISTRICT"><Text>district</Text></Row>
    <Row Tag="LOC_TYPE_TRAIT_YIELD"><Text>{1_Amount: number +#,###;-#,###} {2_YieldIcon} {3_YieldName}</Text></Row>
    <Row Tag="LOC_TYPE_TRAIT_HOUSING"><Text>{1_Amount: number +#,###.#;-#,###.#} [ICON_Housing] Housing</Text></Row>
    <Row Tag="LOC_UI_PEDIA_ERA_SCORE"><Text>Era Score: {1_Num}</Text></Row>
    <Row Tag="LOC_REMOVED_LATER"><Text>This row is deleted by the expansion.</Text></Row>
    <Row Tag="LOC_FRENCH_ONLY" Language="fr_FR"><Text>Pas en anglais</Text></Row>
  </BaseGameText>
</GameData>
"""

PACK_MODINFO = """<?xml version="1.0" encoding="utf-8"?>
<Mod id="11111111-2222-3333-4444-555555555555" version="1">
  <Properties><Name>Test Pack</Name></Properties>
  <InGameActions><UpdateText id="PackText"><File>Text/en_US/Pack_Text.xml</File></UpdateText></InGameActions>
</Mod>
"""

PACK_TEXT = """<?xml version="1.0" encoding="utf-8"?>
<GameData>
  <LocalizedText>
    <Replace Tag="LOC_BUILDING_MOON_GARDEN_DESCRIPTION" Language="en_US"><Text>Pack description of the Moon Garden.</Text></Replace>
    <Replace Tag="LOC_BUILDING_MOON_GARDEN_DESCRIPTION" Language="de_DE"><Text>Deutsch</Text></Replace>
    <Delete Tag="LOC_REMOVED_LATER"/>
    <Update><Where Tag="LOC_TERRAIN_PEAK_NAME"/><Set Text="Mountain Peak"/></Update>
  </LocalizedText>
</GameData>
"""

BALANCE_MODINFO = """<?xml version="1.0" encoding="utf-8"?>
<Mod id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" version="1">
  <Properties><Name>Test Balance Mod</Name></Properties>
</Mod>
"""

BALANCE_TEXT = """<?xml version="1.0" encoding="utf-8"?>
<GameData>
  <LocalizedText>
    <Replace Tag="LOC_DISTRICT_THINKERY_DESCRIPTION" Language="en_US"><Text>Balance mod: the Thinkery now hums with [ICON_Science] Science.</Text></Replace>
  </LocalizedText>
</GameData>
"""

BROKEN_TEXT = "<GameData><LocalizedText><Row Tag='LOC_X'><Text>never closed"

MODDING_LOG = """[100.0] Initializing Modding Framework.
[100.1] Target in-game actions (in order of application):
[100.1] 11111111-2222-3333-4444-555555555555 (Test Pack)
[100.1]  * PackText (UpdateText)
[100.1] aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee (Test Balance Mod)
[100.1]  * UpdateText (UpdateText)
[100.1]  * Broken (UpdateText)
[100.2] Modding Framework - Applying Components
[100.2] Applying Component - PackText (UpdateText)
[100.2] Creating database save point.
[100.2] LocalizedText - Loading Text/en_US/Pack_Text.xml
[100.2] Successfully released save point.
[100.3] Applying Component - UpdateText (UpdateText)
[100.3] LocalizedText - Loading lang/english.xml
[100.3] Applying Component - Broken (UpdateText)
[100.3] LocalizedText - Loading lang/broken.xml
[100.3] Error parsing lang/broken.xml
[100.3] Warning: LocalizedText - Failed loading XML.
[100.4] Modding Framework - Finished Apply Components
[100.5] Modding Framework - Apply Settings
[100.6] Modding Framework - Finished Apply Settings
"""


def _gameplay_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        textwrap.dedent("""
        CREATE TABLE CivilopediaSections(SectionId TEXT, Name TEXT, Icon TEXT, SortIndex INT);
        CREATE TABLE CivilopediaSectionExcludes(SectionId TEXT);
        CREATE TABLE CivilopediaPages(SectionId TEXT, PageId TEXT, PageGroupId TEXT, PageLayoutId TEXT,
            Name TEXT, Tooltip TEXT, SortIndex INT, TextKeyPrefix TEXT);
        CREATE TABLE CivilopediaPageExcludes(SectionId TEXT, PageId TEXT);
        CREATE TABLE CivilopediaPageGroups(SectionId TEXT, PageGroupId TEXT, Name TEXT, Tooltip TEXT,
            VisibleIfEmpty INT, SortIndex INT);
        CREATE TABLE CivilopediaPageGroupQueries(RowId INT, SectionId TEXT, SQL TEXT, PageGroupIdColumn TEXT,
            NameColumn TEXT, TooltipColumn TEXT, VisibleIfEmptyColumn TEXT, SortIndexColumn TEXT, SortIndex INT);
        CREATE TABLE CivilopediaPageQueries(RowId INT, SectionId TEXT, SQL TEXT, PageIdColumn TEXT,
            PageGroupIdColumn TEXT, PageLayoutIdColumn TEXT, NameColumn TEXT, TooltipColumn TEXT,
            TextKeyPrefixColumn TEXT, SortIndexColumn TEXT, SortIndex INT);
        CREATE TABLE CivilopediaPageLayouts(PageLayoutId TEXT, ScriptTemplate TEXT);
        CREATE TABLE CivilopediaPageLayoutChapters(PageLayoutId TEXT, ChapterId TEXT, SortIndex INT);
        CREATE TABLE CivilopediaPageChapterHeaders(SectionId TEXT, PageId TEXT, ChapterId TEXT, Header TEXT);
        CREATE TABLE CivilopediaPageChapterParagraphs(SectionId TEXT, PageId TEXT, ChapterId TEXT,
            Paragraph TEXT, SortIndex INT);
        CREATE TABLE CivilopediaPageSearchTerms(SectionId TEXT, PageId TEXT, Term TEXT);
        CREATE TABLE CivilopediaPageSearchTermQueries(RowId INT, SQL TEXT, SectionIdColumn TEXT,
            PageIdColumn TEXT, SearchTermColumn TEXT);

        CREATE TABLE Yields(YieldType TEXT, Name TEXT, IconString TEXT);
        CREATE TABLE Terrains(TerrainType TEXT, Name TEXT);
        CREATE TABLE Features(FeatureType TEXT, Name TEXT);
        CREATE TABLE Technologies(TechnologyType TEXT, Name TEXT, Cost INT, EraType TEXT, Description TEXT);
        CREATE TABLE Civics(CivicType TEXT, Name TEXT);
        CREATE TABLE Districts(DistrictType TEXT, Name TEXT, Description TEXT, PrereqTech TEXT, PrereqCivic TEXT,
            Cost INT, Maintenance INT, Coast INT, Aqueduct INT, NoAdjacentCity INT, TraitType TEXT,
            Housing INT, Entertainment INT, CitizenSlots INT, AirSlots INT, Appeal INT, PlunderType TEXT,
            InternalOnly INT);
        CREATE TABLE District_ValidTerrains(DistrictType TEXT, TerrainType TEXT);
        CREATE TABLE District_RequiredFeatures(DistrictType TEXT, FeatureType TEXT);
        CREATE TABLE District_Adjacencies(DistrictType TEXT, YieldChangeId TEXT);
        CREATE TABLE Adjacency_YieldChanges(ID TEXT, YieldType TEXT, YieldChange INT, TilesRequired INT,
            OtherDistrictAdjacent INT, AdjacentTerrain TEXT, AdjacentFeature TEXT, AdjacentImprovement TEXT,
            AdjacentDistrict TEXT, AdjacentResource INT, AdjacentSeaResource INT, AdjacentResourceClass TEXT,
            AdjacentRiver INT, AdjacentWonder INT, AdjacentNaturalWonder INT, PrereqCivic TEXT, PrereqTech TEXT,
            ObsoleteCivic TEXT, ObsoleteTech TEXT);
        CREATE TABLE DistrictReplaces(CivUniqueDistrictType TEXT, ReplacesDistrictType TEXT);
        CREATE TABLE Buildings(BuildingType TEXT, Name TEXT, Description TEXT, PrereqDistrict TEXT, PrereqTech TEXT,
            PrereqCivic TEXT, Cost INT, IsWonder INT, InternalOnly INT, TraitType TEXT, Housing INT);
        CREATE TABLE Moments(MomentType TEXT, Name TEXT, Description TEXT, EraScore INT, InterestLevel INT);
    """)
    )
    db.executemany(
        "INSERT INTO CivilopediaSections VALUES (?,?,?,?)",
        [
            ("CONCEPTS", "LOC_SECTION_IDEAS", None, 0),
            ("PLACES", "LOC_SECTION_PLACES", None, 10),
            ("MOMENTS", "LOC_SECTION_MOMENTS", None, 20),
        ],
    )
    db.execute(
        "INSERT INTO CivilopediaPages VALUES ('CONCEPTS','DISTRICTS',NULL,'Simple','LOC_PAGE_IDEA_DISTRICTS_TITLE',NULL,0,NULL)"
    )
    db.executemany(
        "INSERT INTO CivilopediaPageQueries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "PLACES",
                "SELECT DistrictType AS PageId, NULL AS PageGroupId, 'District' AS PageLayoutId, Name "
                "FROM Districts WHERE InternalOnly = 0",
                "PageId",
                "PageGroupId",
                "PageLayoutId",
                "Name",
                "Tooltip",
                None,
                None,
                10,
            ),
            (
                2,
                "PLACES",
                "SELECT BuildingType AS PageId, 'Wonders' AS PageGroupId, 'Building' AS PageLayoutId, Name FROM Buildings",
                "PageId",
                "PageGroupId",
                "PageLayoutId",
                "Name",
                "Tooltip",
                None,
                None,
                10,
            ),
            (
                3,
                "MOMENTS",
                "SELECT MomentType AS PageId, NULL AS PageGroupId, 'HistoricMoment' AS PageLayoutId, Name FROM Moments",
                "PageId",
                "PageGroupId",
                "PageLayoutId",
                "Name",
                "Tooltip",
                None,
                None,
                10,
            ),
        ],
    )
    db.executemany(
        "INSERT INTO CivilopediaPageLayouts VALUES (?,?)",
        [("Simple", "Simple"), ("District", "District"), ("Building", "Building"), ("HistoricMoment", "HistoricMoment")],
    )
    db.executemany(
        "INSERT INTO CivilopediaPageLayoutChapters VALUES (?,?,?)",
        [("Simple", "CONTENT", 10), ("District", "HISTORY", 10), ("Building", "HISTORY", 10)],
    )
    db.execute("INSERT INTO CivilopediaPageSearchTerms VALUES ('PLACES','DISTRICT_THINKERY','LOC_SEARCH_TERM_BRAINS')")
    db.executemany(
        "INSERT INTO Yields VALUES (?,?,?)",
        [
            ("YIELD_SCIENCE", "LOC_YIELD_SCIENCE_NAME", "[ICON_Science]"),
            ("YIELD_PRODUCTION", "LOC_YIELD_PRODUCTION_NAME", "[ICON_Production]"),
            ("YIELD_GOLD", "LOC_YIELD_GOLD_NAME", "[ICON_Gold]"),
        ],
    )
    db.executemany(
        "INSERT INTO Terrains VALUES (?,?)",
        [("TERRAIN_MEADOW", "LOC_TERRAIN_MEADOW_NAME"), ("TERRAIN_PEAK", "LOC_TERRAIN_PEAK_NAME")],
    )
    db.execute("INSERT INTO Technologies VALUES ('TECH_SCRIBBLING','LOC_TECH_SCRIBBLING_NAME',20,'ERA_TEST',NULL)")
    db.executemany(
        "INSERT INTO Districts (DistrictType, Name, Description, PrereqTech, Cost, Maintenance, Coast, "
        "InternalOnly, Housing) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "DISTRICT_THINKERY",
                "LOC_DISTRICT_THINKERY_NAME",
                "LOC_DISTRICT_THINKERY_DESCRIPTION",
                "TECH_SCRIBBLING",
                54,
                1,
                0,
                0,
                0,
            ),
            ("DISTRICT_WATERWORKS", "LOC_DISTRICT_WATERWORKS_NAME", "LOC_DISTRICT_WATERWORKS_DESCRIPTION", None, 30, 0, 1, 0, 2),
            ("DISTRICT_HANZEL", "LOC_DISTRICT_HANZEL_NAME", "LOC_DISTRICT_HANZEL_DESCRIPTION", None, 40, 0, 0, 0, 0),
            ("DISTRICT_MBOLO", "LOC_DISTRICT_MBOLO_NAME", "LOC_DISTRICT_MBOLO_DESCRIPTION", None, 40, 0, 0, 0, 0),
            ("DISTRICT_EMBOLDEN", "LOC_DISTRICT_EMBOLDEN_NAME", "LOC_DISTRICT_EMBOLDEN_DESCRIPTION", None, 40, 0, 0, 0, 0),
        ],
    )
    db.execute("INSERT INTO District_ValidTerrains VALUES ('DISTRICT_THINKERY','TERRAIN_MEADOW')")
    db.executemany(
        "INSERT INTO District_Adjacencies VALUES (?,?)",
        [("DISTRICT_THINKERY", "Peak_Science"), ("DISTRICT_THINKERY", "District_Science")],
    )
    db.executemany(
        "INSERT INTO Adjacency_YieldChanges (ID, YieldType, YieldChange, TilesRequired, "
        "OtherDistrictAdjacent, AdjacentTerrain, AdjacentResourceClass) VALUES (?,?,?,?,?,?,?)",
        [
            ("Peak_Science", "YIELD_SCIENCE", 2, 1, 0, "TERRAIN_PEAK", "NO_RESOURCECLASS"),
            ("District_Science", "YIELD_SCIENCE", 1, 2, 1, None, "NO_RESOURCECLASS"),
        ],
    )
    db.execute(
        "INSERT INTO Buildings VALUES ('BUILDING_MOON_GARDEN','LOC_BUILDING_MOON_GARDEN_NAME',"
        "'LOC_BUILDING_MOON_GARDEN_DESCRIPTION','DISTRICT_THINKERY',NULL,NULL,90,0,0,NULL,0)"
    )
    db.execute("INSERT INTO Moments VALUES ('MOMENT_FIRST_THINKERY','LOC_MOMENT_FIRST_THINKERY_NAME',NULL,3,2)")
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def private_dirs(tmp_path_factory, monkeypatch):
    """Tests never read or write the real index, key, spend ledger or latency log."""
    home = tmp_path_factory.mktemp("civspeak-home")
    monkeypatch.setenv("CIVSPEAK_DATA_DIR", str(home / "data"))
    monkeypatch.setenv("CIVSPEAK_CONFIG_DIR", str(home / "config"))


@pytest.fixture
def fake_game(tmp_path: Path) -> GamePaths:
    game = tmp_path / "steamapps" / "common" / "Game"
    text_dir = game / "Base" / "Assets" / "Text" / "en_US"
    text_dir.mkdir(parents=True)
    (text_dir / "Test_Text.xml").write_text(BASE_TEXT, encoding="utf-8")

    pack = game / "DLC" / "TestPack"
    (pack / "Text" / "en_US").mkdir(parents=True)
    (pack / "TestPack.modinfo").write_text(PACK_MODINFO, encoding="utf-8")
    (pack / "Text" / "en_US" / "Pack_Text.xml").write_text(PACK_TEXT, encoding="utf-8")

    workshop = tmp_path / "steamapps" / "workshop" / "content" / "289070" / "123"
    (workshop / "lang").mkdir(parents=True)
    (workshop / "BalanceMod.modinfo").write_text(BALANCE_MODINFO, encoding="utf-8")
    # Mixed case on purpose: the log's paths are matched case-insensitively, like on Windows.
    (workshop / "lang" / "English.xml").write_text(BALANCE_TEXT, encoding="utf-8")
    (workshop / "lang" / "broken.xml").write_text(BROKEN_TEXT, encoding="utf-8")

    user = tmp_path / "Firaxis"
    (user / "Cache").mkdir(parents=True)
    (user / "Logs").mkdir(parents=True)
    _gameplay_db(user / "Cache" / "DebugGameplay.sqlite")
    (user / "Logs" / "Modding.log").write_text(MODDING_LOG, encoding="utf-8")
    return GamePaths(game_dir=game, user_dir=user, mod_roots=[game / "DLC", workshop.parent])


@pytest.fixture
def built_index(fake_game, tmp_path):
    from civspeak import index

    report = index.build(fake_game, tmp_path / "data")
    return report.index, report
