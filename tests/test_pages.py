"""The stats box of each page type, built from small invented databases.

Like conftest.py, every name and sentence here is made up; only the table and column names and the
text-key patterns mirror the game, because those are what pedia.py reads.
"""

import sqlite3

from civspeak.loc import Localizer
from civspeak.pedia import Page, Pedia, adjacencies, table_units

TEXT = {
    "LOC_UI_PEDIA_TRAITS": "Traits",
    "LOC_UI_PEDIA_USAGE": "Usage",
    "LOC_UI_PEDIA_REQUIREMENTS": "Requirements",
    "LOC_UI_PEDIA_UNIQUE_ABILITY": "Unique Ability",
    "LOC_UI_PEDIA_GREATPERSON_ACTION": "{1_Name} ({2_Charges} charge)",
    "LOC_GREATPERSON_ACTION_NAME_DEFAULT": "Active",
    "LOC_UI_PEDIA_CREATES": "Creates",
    "LOC_UI_PEDIA_CREATED_BY": "Created By",
    "LOC_UI_PEDIA_UPGRADES_TO": "Upgrades To",
    "LOC_UI_PEDIA_UPGRADE_FROM": "Upgrade From",
    "LOC_UI_PEDIA_MOVEMENT_POINTS": "Moves",
    "LOC_UI_PEDIA_MELEE_STRENGTH": "Strength",
    "LOC_HUD_PRODUCTION_COST": "Cost",
    "LOC_UI_PEDIA_REPLACES": "Replaces",
    "LOC_UI_PEDIA_ESPIONAGE_MISSIONS": "Missions",
    "LOC_TYPE_TRAIT_BASE_PROBABILITY": "Odds: {1_Num}%",
    "LOC_UI_PEDIA_BUILT_BY": "Built By",
    "LOC_UI_PEDIA_FOLLOWERS": "Followers",
    "LOC_UI_PEDIA_TOURISM_BOMB_VALUE_BUILDING": "+{1_Tourism} Tourism from concerts.",
    "LOC_OPTIONS": "Options",
    "LOC_UI_PEDIA_APPLIES_TO": "Applies To",
    "LOC_DIPLOACTION_ALLIANCE_LEVEL": "Level {1_Level}",
    "LOC_TUTORIAL_GOAL_GENERAL_1": "Goals:",
    "LOC_UI_PEDIA_UNLOCKS": "Unlocks",
    "LOC_YIELD_HUMOUR_NAME": "Humour",
    "LOC_ERA_DAWN_NAME": "Dawn Era",
    "LOC_ERA_DUSK_NAME": "Dusk Era",
    "LOC_CIV_PLUMLAND_NAME": "Plumland",
    "LOC_CIV_FIGLAND_NAME": "Figland",
    "LOC_GP_JESTER_NAME": "Great Jester",
    "LOC_GP_BOBO_NAME": "Bobo the Bold",
    "LOC_GP_SIR_NAME": "Sir Timeless",
    "LOC_GOLD_PLEASE": "Gain {Amount : number #} Humour now.",
    "LOC_RES_KAZOO_NAME": "Kazoos",
    "LOC_UNIT_STICK_NAME": "Stick Fighter",
    "LOC_UNIT_POLE_NAME": "Pole Fighter",
    "LOC_UNIT_PLUM_POLE_NAME": "Plum Poler",
    "LOC_UNIT_FIG_POLE_NAME": "Fig Poler",
    "LOC_UNIT_SNOOP_NAME": "Snoop",
    "LOC_UNIT_PEDDLER_NAME": "Peddler",
    "LOC_OP_PEEK_NAME": "Peek",
    "LOC_ROUTE_PATH_NAME": "Footpath",
    "LOC_ROUTE_RAIL_NAME": "Rail",
    "LOC_REL_DOTS_NAME": "Dotism",
    "LOC_LEADER_PLUM_NAME": "Queen Plum",
    "LOC_WONDER_TOWER_NAME": "Silly Tower",
    "LOC_RES_A_NAME": "Option A Name",
    "LOC_RES_PIE_NAME": "Pie Vote",
    "LOC_RES_PIE_A": "Everyone gets pie.",
    "LOC_RES_PIE_B": "Nobody gets pie.",
    "LOC_ALLY_SNACK_NAME": "Snack Alliance",
    "LOC_SNACK_1": "+1 Humour on shared snacks.",
    "LOC_SNACK_2": "Snacks are free.",
    "LOC_DISC_NAME": "Pie Emergency",
    "LOC_EMERGENCY_PIE_NAME": "Pie Emergency",
    "LOC_EM_GOAL": "Members must take back {1_CityName} from {TargetName}.",
    "LOC_DISTRICT_LAB_NAME": "Giggle Lab",
    "LOC_DISTRICT_PLUM_LAB_NAME": "Plum Lab",
    "LOC_FEATURE_SWAMP_NAME": "Swamp",
    "LOC_TYPE_TRAIT_ADJACENT_BONUS": (
        "{1_Amount: number +#;-#} {2_YieldIcon} {3_YieldName} from each adjacent {5_AdjacentObject} tile."
    ),
    "LOC_HERO_PROJECT_UNIT": "x",
    "LOC_UNIT_HERO_ZORRO_NAME": "Zorro",
}


def make_db(tables: dict[str, list[dict]]) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    for name, rows in tables.items():
        cols = list(dict.fromkeys(c for r in rows for c in r))
        db.execute(f"CREATE TABLE {name} ({', '.join(cols)})")
        for r in rows:
            db.execute(f"INSERT INTO {name} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))
    return db


def build(layout, page_id, tables, section="TEST") -> Page:
    from civspeak.pedia import LAYOUTS

    page = Page(section_id=section, section="Test", page_id=page_id, layout=layout, title=page_id)
    LAYOUTS[layout](Pedia(make_db(tables), Localizer(TEXT)), page)
    return page


def facts(page: Page) -> dict[str, list[str]]:
    return {g.heading: g.lines for g in page.facts if g.lines}


ERAS = [
    {"EraType": "ERA_DAWN", "Name": "LOC_ERA_DAWN_NAME", "ChronologyIndex": 1},
    {"EraType": "ERA_DUSK", "Name": "LOC_ERA_DUSK_NAME", "ChronologyIndex": 2},
]
YIELDS = [{"YieldType": "YIELD_HUMOUR", "Name": "LOC_YIELD_HUMOUR_NAME", "IconString": "[ICON_Humour]"}]
JESTER = {
    "GreatPersonClassType": "GP_JESTER",
    "Name": "LOC_GP_JESTER_NAME",
    "UnitType": "UNIT_JESTER",
    "AvailableInTimeline": 1,
    "IconString": "",
}
GIFT_MODIFIERS = {
    "Modifiers": [
        {"ModifierId": "BOBO_GIFT", "ModifierType": "GRANT_RES"},
        {"ModifierId": "BOBO_LAUGH", "ModifierType": "LAUGH"},
    ],
    "DynamicModifiers": [
        {"ModifierType": "GRANT_RES", "EffectType": "EFFECT_GRANT_FREE_RESOURCE_IN_CITY"},
        {"ModifierType": "LAUGH", "EffectType": "EFFECT_LAUGH"},
    ],
    "ModifierArguments": [
        {"ModifierId": "BOBO_GIFT", "Name": "ResourceType", "Value": "RESOURCE_KAZOO"},
        {"ModifierId": "BOBO_LAUGH", "Name": "Amount", "Value": "40"},
    ],
    "ModifierStrings": [{"ModifierId": "BOBO_LAUGH", "Context": "Summary", "Text": "LOC_GOLD_PLEASE"}],
    "GreatPersonIndividualActionModifiers": [
        {"GreatPersonIndividualType": "GP_BOBO", "ModifierId": "BOBO_GIFT"},
        {"GreatPersonIndividualType": "GP_BOBO", "ModifierId": "BOBO_LAUGH"},
    ],
    "GreatPersonIndividuals": [
        {
            "GreatPersonIndividualType": "GP_BOBO",
            "Name": "LOC_GP_BOBO_NAME",
            "GreatPersonClassType": "GP_JESTER",
            "EraType": "ERA_DAWN",
            "ActionCharges": 1,
        },
        {
            "GreatPersonIndividualType": "GP_SIR",
            "Name": "LOC_GP_SIR_NAME",
            "GreatPersonClassType": "GP_TIMELESS",
            "EraType": "ERA_DAWN",
            "ActionCharges": 0,
        },
    ],
    "GreatPersonClasses": [JESTER, {**JESTER, "GreatPersonClassType": "GP_TIMELESS", "AvailableInTimeline": 0}],
    "Resources": [{"ResourceType": "RESOURCE_KAZOO", "Name": "LOC_RES_KAZOO_NAME"}],
    "Eras": ERAS,
}


def test_great_person_abilities_come_from_their_modifiers_with_named_arguments():
    page = build("GreatPerson", "GP_BOBO", GIFT_MODIFIERS)
    assert facts(page)["Traits"] == ["Dawn Era", "Great Jester"]
    assert facts(page)["Creates"] == ["Kazoos"]
    assert page.chapters[0].title == "Unique Ability: Active (1 charge)"
    assert page.chapters[0].paragraphs == ["Gain 40 Humour now."]
    # A class outside the timeline shows no era, as in the game.
    assert "Dawn Era" not in facts(build("GreatPerson", "GP_SIR", GIFT_MODIFIERS))["Traits"]
    # The resource names who creates it.
    resource = build("Resource", "RESOURCE_KAZOO", GIFT_MODIFIERS)
    assert facts(resource)["Created By"] == ["Bobo the Bold"]


UNITS = {
    "Units": [
        {"UnitType": "UNIT_STICK", "Name": "LOC_UNIT_STICK_NAME", "Cost": 10, "BaseMoves": 2, "Combat": 5},
        {"UnitType": "UNIT_POLE", "Name": "LOC_UNIT_POLE_NAME", "Cost": 20, "BaseMoves": 2, "Combat": 9},
        {"UnitType": "UNIT_PLUM_POLE", "Name": "LOC_UNIT_PLUM_POLE_NAME", "Cost": 25, "Combat": 11, "TraitType": "TRAIT_PLUM"},
        {"UnitType": "UNIT_FIG_POLE", "Name": "LOC_UNIT_FIG_POLE_NAME", "Cost": 25, "Combat": 10, "TraitType": "TRAIT_FIG"},
        {"UnitType": "UNIT_SNOOP", "Name": "LOC_UNIT_SNOOP_NAME", "BaseMoves": 3, "IgnoreMoves": 1, "Spy": 1},
    ],
    "UnitUpgrades": [{"Unit": "UNIT_STICK", "UpgradeUnit": "UNIT_POLE"}],
    "UnitReplaces": [
        {"CivUniqueUnitType": "UNIT_PLUM_POLE", "ReplacesUnitType": "UNIT_POLE"},
        {"CivUniqueUnitType": "UNIT_FIG_POLE", "ReplacesUnitType": "UNIT_POLE"},
    ],
    "CivilizationTraits": [
        {"CivilizationType": "CIV_PLUM", "TraitType": "TRAIT_PLUM"},
        {"CivilizationType": "CIV_FIG", "TraitType": "TRAIT_FIG"},
    ],
    "Civilizations": [
        {"CivilizationType": "CIV_PLUM", "Name": "LOC_CIV_PLUMLAND_NAME"},
        {"CivilizationType": "CIV_FIG", "Name": "LOC_CIV_FIGLAND_NAME"},
    ],
    "UnitOperations": [
        {
            "Description": "LOC_OP_PEEK_NAME",
            "InterfaceMode": "INTERFACEMODE_SPY_CHOOSE_MISSION",
            "Turns": 0,
            "BaseProbability": 11,
        }
    ],
}


def test_upgrade_paths_include_unique_units_of_the_right_civilization():
    assert facts(build("Unit", "UNIT_STICK", UNITS))["Upgrades To"] == ["Pole Fighter", "Plum Poler", "Fig Poler"]
    # Plumland's unique unit upgrades from the unit its base unit does, and sees no other civ's uniques.
    plum = facts(build("Unit", "UNIT_PLUM_POLE", UNITS))
    assert plum["Upgrade From"] == ["Stick Fighter"]


def test_spies_show_missions_and_no_movement():
    snoop = facts(build("Unit", "UNIT_SNOOP", UNITS))
    assert not any("Moves" in line for line in snoop.get("Traits", []))
    assert snoop["Missions"] == ["Peek (Odds: 50%)"]  # 11 or more on three dice


def test_unit_table_groups_by_era_and_marks_unique_units():
    tables = {
        **UNITS,
        "Units": [dict(u, FormationClass="FORMATION_CLASS_LAND_COMBAT") for u in UNITS["Units"][:4]],
        "Eras": ERAS,
    }
    page = Page("UNITS", "Units", "MELEE", "TableUnits", "Melee")
    table_units(Pedia(make_db(tables), Localizer(TEXT)), page)
    assert page.facts[0].heading == "Dawn Era"
    assert page.facts[0].lines[:2] == [
        "Stick Fighter: Moves 2, Strength 5, Cost 10",
        "Pole Fighter: Moves 2, Strength 9, Cost 20",
    ]
    assert "Plum Poler (Plumland, Replaces Pole Fighter): Strength 11, Cost 25" in page.facts[0].lines


def test_routes_say_traders_build_them_unless_reserved_for_a_unit():
    tables = {
        "Routes": [
            {"RouteType": "ROUTE_PATH", "Name": "LOC_ROUTE_PATH_NAME"},
            {"RouteType": "ROUTE_RAIL", "Name": "LOC_ROUTE_RAIL_NAME"},
        ],
        "Routes_XP2": [{"RouteType": "ROUTE_RAIL", "BuildOnlyWithUnit": 1}],
        "Route_ValidBuildUnits": [{"RouteType": "ROUTE_RAIL", "UnitType": "UNIT_STICK"}],
        "Units": [{"UnitType": "UNIT_TRADER", "Name": "LOC_UNIT_PEDDLER_NAME"}, *UNITS["Units"][:1]],
    }
    assert facts(build("Route", "ROUTE_PATH", tables))["Built By"] == ["Peddler"]
    assert facts(build("Route", "ROUTE_RAIL", tables))["Built By"] == ["Stick Fighter"]


def test_religion_lists_its_followers_and_every_wonder_can_host_a_concert():
    tables = {
        "FavoredReligions": [{"ReligionType": "REL_DOTS", "LeaderType": "LEADER_PLUM"}],
        "Leaders": [{"LeaderType": "LEADER_PLUM", "Name": "LOC_LEADER_PLUM_NAME"}],
    }
    assert facts(build("Religion", "REL_DOTS", tables))["Followers"] == ["Queen Plum"]
    tower = {
        "Buildings": [{"BuildingType": "WONDER_TOWER", "Name": "LOC_WONDER_TOWER_NAME", "MaxWorldInstances": 1, "Cost": 0}],
        "GlobalParameters": [{"Name": "TOURISM_BOMB_WONDER_ADDITIONAL", "Value": "700"}],
    }
    assert "+700 Tourism from concerts." in facts(build("Building", "WONDER_TOWER", tower))["Traits"]


def test_world_congress_alliance_and_emergency_pages():
    tables = {
        "Resolutions": [
            {
                "ResolutionType": "RES_PIE",
                "Effect1Description": "LOC_RES_PIE_A",
                "Effect2Description": "LOC_RES_PIE_B",
                "EarliestEra": "ERA_DUSK",
            }
        ],
        "Eras": ERAS,
        "Alliances": [{"AllianceType": "ALLY_SNACK", "Name": "LOC_ALLY_SNACK_NAME"}],
        "AllianceEffects": [
            {"AllianceType": "ALLY_SNACK", "LevelRequirement": 1, "ModifierID": "SNACK_1"},
            {"AllianceType": "ALLY_SNACK", "LevelRequirement": 2, "ModifierID": "SNACK_2"},
        ],
        "ModifierStrings": [
            {"ModifierId": "SNACK_1", "Context": "Summary", "Text": "LOC_SNACK_1"},
            {"ModifierId": "SNACK_2", "Context": "Summary", "Text": "LOC_SNACK_2"},
        ],
        "Discussions": [{"DiscussionType": "DISC_PIE", "Name": "LOC_DISC_NAME", "EmergencyType": "EM_PIE"}],
        "EmergencyAlliances": [{"EmergencyType": "EM_PIE", "Name": "LOC_EMERGENCY_PIE_NAME", "GoalText": "PieGoal"}],
        "EmergencyGoalTexts": [{"GoalType": "PieGoal", "GoalDescription": "LOC_EM_GOAL"}],
    }
    resolution = facts(build("Resolution", "RES_PIE", tables))
    assert resolution["Options"] == ["A: Everyone gets pie.", "B: Nobody gets pie."]
    assert resolution["Applies To"] == ["Dusk Era"]
    assert facts(build("Alliance", "ALLY_SNACK", tables)) == {
        "Level 1": ["+1 Humour on shared snacks."],
        "Level 2": ["Snacks are free."],
    }
    # A live emergency's names are filled in by the game; the page says who in words instead.
    assert facts(build("Discussion", "DISC_PIE", tables))["Goals"] == ["Members must take back the city from the Target."]


def test_adjacency_overview_lists_standard_districts_before_unique_ones():
    tables = {
        "Districts": [
            {"DistrictType": "DISTRICT_PLUM_LAB", "Name": "LOC_DISTRICT_PLUM_LAB_NAME", "TraitType": "TRAIT_PLUM"},
            {"DistrictType": "DISTRICT_LAB", "Name": "LOC_DISTRICT_LAB_NAME"},
        ],
        "District_Adjacencies": [
            {"DistrictType": "DISTRICT_LAB", "YieldChangeId": "Swamp_Humour"},
            {"DistrictType": "DISTRICT_PLUM_LAB", "YieldChangeId": "Swamp_Humour"},
        ],
        "Adjacency_YieldChanges": [
            {
                "ID": "Swamp_Humour",
                "YieldType": "YIELD_HUMOUR",
                "YieldChange": 2,
                "TilesRequired": 1,
                "AdjacentFeature": "FEATURE_SWAMP",
            }
        ],
        "Features": [{"FeatureType": "FEATURE_SWAMP", "Name": "LOC_FEATURE_SWAMP_NAME"}],
        "Yields": YIELDS,
        "CivilizationTraits": UNITS["CivilizationTraits"],
        "Civilizations": UNITS["Civilizations"],
    }
    page = Page("DISTRICTS", "Districts", "ADJACENCIES", "Adjacencies", "Adjacency")
    adjacencies(Pedia(make_db(tables), Localizer(TEXT)), page)
    assert [(g.heading, g.lines) for g in page.facts] == [
        ("Giggle Lab", ["+2 Humour from each adjacent Swamp tile."]),
        ("Plum Lab (Plumland)", ["+2 Humour from each adjacent Swamp tile."]),
    ]


def test_hero_projects_name_the_hero_they_unlock():
    tables = {"Projects": [{"ProjectType": "PROJECT_CREATE_HERO_ZORRO", "Cost": 5, "CostProgressionModel": "GROWS"}]}
    page = facts(build("Project", "PROJECT_CREATE_HERO_ZORRO", tables))
    assert page["Unlocks"] == ["Zorro"]
    assert "Production Cost" not in page  # a cost that grows is not shown as a fixed number, as in the game
