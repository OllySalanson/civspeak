"""Placeholder fill and markup cleanup."""

from civspeak.loc import MISSING, Localizer, clean

TAGS = {
    "LOC_NAME": "Thinkery",
    "LOC_YIELD": "Science",
    "LOC_BONUS": "{1_Amount: number +#,###.#;-#,###.#} {2_Icon} {3_Name} from {4_Num : plural 1?a tile; other?{4_Num} tiles;}.",
    "LOC_COST": "Base Cost: {1_Amount : number #} {2_Icon} {3_Name}",
    "LOC_NESTED": "Build a {LOC_NAME} first.",
    "LOC_GRAMMAR": "{LOC_NAME << {1_CivName}} rules",
    "LOC_GOV": "{LOC_NAME} ({LOC_YIELD})",
    "LOC_LOOP": "{LOC_LOOP}",
    "LOC_ARGS": "{1_X} and {2_Y}",
}


def test_number_formats_icons_and_plurals():
    loc = Localizer(TAGS)
    assert loc.lookup("LOC_BONUS", 2, "[ICON_Science]", "LOC_YIELD", 1) == "+2 [ICON_Science] Science from a tile."
    assert loc.lookup("LOC_BONUS", -0.5, "[ICON_Science]", "LOC_YIELD", 3) == "-0.5 [ICON_Science] Science from 3 tiles."
    assert loc.lookup("LOC_COST", 54, "[ICON_Production]", "Production") == "Base Cost: 54 [ICON_Production] Production"


def test_nested_tags_grammar_and_runaway_recursion():
    loc = Localizer(TAGS)
    assert loc.lookup("LOC_NESTED") == "Build a Thinkery first."
    assert loc.lookup("LOC_GOV") == "Thinkery (Science)"
    assert loc.lookup("LOC_GRAMMAR", "Testland") == "Testland rules"
    assert loc.lookup("LOC_LOOP") == ""


def test_missing_args_and_unknown_tags():
    loc = Localizer(TAGS)
    assert loc.lookup("LOC_ARGS", "one") == f"one and {MISSING}"
    assert loc.lookup("LOC_DISTRICT_MADE_UP_NAME") == "District Made Up"
    assert loc.lookup("plain text") == "plain text"


def test_clean_turns_markup_into_plain_text():
    assert clean("+2 [ICON_Science] Science[NEWLINE][ICON_Bullet]Next") == "+2 Science\n• Next"
    assert clean("[COLOR_Red]Warning[ENDCOLOR] +3 [ICON_Strength]") == "Warning +3 Combat Strength"
    assert clean("+1 [ICON_GreatScientist] Great Scientist point") == "+1 Great Scientist point"
    assert clean("Needs [ICON_RESOURCE_IRON] here") == "Needs Iron here"
