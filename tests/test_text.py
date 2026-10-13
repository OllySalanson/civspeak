"""Modding.log replay, mod folder resolution and text-table rebuild."""

from civspeak import modlog, mods
from civspeak.textdb import TextDB
from conftest import MODDING_LOG


def test_log_uses_last_in_game_setup_and_drops_failed_files():
    old = MODDING_LOG.replace("Pack_Text.xml", "Old_Text.xml")
    session = modlog.parse(old + MODDING_LOG)
    files = [f for t in session.texts for f in t.files]
    assert files == ["Text/en_US/Pack_Text.xml", "lang/english.xml"]
    failed = [f for t in session.texts for f in t.failed]
    assert failed == ["lang/broken.xml"]
    assert session.mods == {
        "11111111-2222-3333-4444-555555555555": "Test Pack",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "Test Balance Mod",
    }


def test_log_without_a_game_says_what_to_do():
    try:
        modlog.parse("[1.0] Initializing Modding Framework.\n")
    except modlog.LogError as e:
        assert "load a game" in str(e)
    else:
        raise AssertionError("expected LogError")


def test_text_files_resolve_to_their_mod_folders_case_insensitively(fake_game):
    session = modlog.parse(fake_game.modding_log.read_text())
    plan = mods.plan_text(session, mods.scan_modinfos(fake_game.mod_roots))
    names = [(f.path.name, f.mod_id) for f in plan.files]
    assert names == [
        ("Pack_Text.xml", "11111111-2222-3333-4444-555555555555"),
        ("English.xml", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    ]
    assert plan.unresolved == []
    assert plan.failed_in_game == ["lang/broken.xml"]


def test_duplicate_component_ids_go_to_the_mod_that_has_the_file(tmp_path):
    for mid, name in (("aaaaaaaa-0000-0000-0000-000000000001", "one"), ("aaaaaaaa-0000-0000-0000-000000000002", "two")):
        d = tmp_path / "mods" / name
        (d / "Text").mkdir(parents=True)
        (d / f"{name}.modinfo").write_text(f'<Mod id="{mid}"><Properties><Name>{name}</Name></Properties></Mod>')
        (d / "Text" / f"{name}.xml").write_text("<GameData/>")
    log = (
        "Target in-game actions (in order of application):\n"
        "aaaaaaaa-0000-0000-0000-000000000001 (one)\n * UpdateText (UpdateText)\n"
        "aaaaaaaa-0000-0000-0000-000000000002 (two)\n * UpdateText (UpdateText)\n"
        "Modding Framework - Applying Components\n"
        "Applying Component - UpdateText (UpdateText)\nLocalizedText - Loading Text/two.xml\n"
        "Applying Component - UpdateText (UpdateText)\nLocalizedText - Loading Text/one.xml\n"
        "Modding Framework - Finished Apply Components\n"
    )
    plan = mods.plan_text(modlog.parse(log), mods.scan_modinfos([tmp_path / "mods"]))
    assert [(f.path.parent.parent.name, f.path.name) for f in plan.files] == [("two", "two.xml"), ("one", "one.xml")]


def test_row_replace_delete_update_and_english_only(fake_game, tmp_path):
    text = TextDB()
    assert text.load(fake_game.base_text_dir / "Test_Text.xml")
    assert text.load(fake_game.game_dir / "DLC" / "TestPack" / "Text" / "en_US" / "Pack_Text.xml")
    tags = text.tags()
    assert tags["LOC_BUILDING_MOON_GARDEN_DESCRIPTION"] == "Pack description of the Moon Garden."
    assert "LOC_REMOVED_LATER" not in tags
    assert tags["LOC_TERRAIN_PEAK_NAME"] == "Mountain Peak"
    assert "LOC_FRENCH_ONLY" not in tags


def test_sql_text_files_and_bad_files_leave_no_partial_state(tmp_path):
    sql = tmp_path / "text.sql"
    sql.write_text(
        "-- made-up test text\n"
        "INSERT INTO LocalizedText (Language, Tag, Text) VALUES ('en_US', 'LOC_A', 'Alpha; with a semicolon');\n"
        "INSERT INTO BaseGameText (Tag, Text) VALUES ('LOC_B', 'Beta');\n"
        "INSERT INTO LocalizedText (Language, Tag, Text) VALUES ('fr_FR', 'LOC_C', 'Gamma');\n"
    )
    bad = tmp_path / "bad.sql"
    bad.write_text("INSERT INTO LocalizedText (Language, Tag, Text) VALUES ('en_US', 'LOC_D', 'Delta');\nNOT SQL;\n")
    text = TextDB()
    assert text.load(sql)
    assert not text.load(bad)
    assert text.tags() == {"LOC_A": "Alpha; with a semicolon", "LOC_B": "Beta"}
