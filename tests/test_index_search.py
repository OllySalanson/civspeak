"""End to end: build the index from the fake game, then search it like the CLI does."""

import json
import os
import sqlite3

from civspeak import index
from civspeak.phonetic import phrase_key
from civspeak.search import Index, entry_text


def titles(hits):
    return [h.page["title"] for h in hits]


def test_build_reports_pages_and_mod_text_wins(built_index):
    out, report = built_index
    assert report.pages == 8  # concept, 5 districts, 1 building, 1 moment
    assert report.unresolved == []
    db = sqlite3.connect(out)
    text = dict(db.execute("SELECT title, text FROM pages"))
    assert "Balance mod: the Thinkery now hums with Science." in text["Thinkery"]
    assert "Pack description of the Moon Garden." in text["Moon Garden"]


def test_district_facts_are_filled_like_the_game_shows_them(built_index):
    out, _ = built_index
    idx = Index(out)
    page = idx.search("thinkery")[0].page
    facts = {g["heading"]: g["lines"] for g in page["facts"]}
    assert facts["Adjacency Bonus"] == [
        "+2 Science from each adjacent Mountain Peak tile.",
        "+1 Science from every 2 adjacent district tiles.",
    ]
    assert facts["Placement"] == ["Meadow"]
    assert facts["Technology"] == ["Scribbling"]
    assert facts["Production Cost"] == ["Base Cost: 54 Production"]
    assert facts["Unlocks Buildings"] == ["Moon Garden"]
    chapters = {c["title"]: c["paragraphs"] for c in page["chapters"]}
    assert chapters["Historical Context"] == ["Long ago, pretend scholars met here.", "They argued\na lot."]
    water = idx.search("waterworks")[0].page
    assert {g["heading"]: g["lines"] for g in water["facts"]}["Placement"] == ["Must be next to the coast"]


def test_exact_fuzzy_soundalike_and_search_terms(built_index):
    out, _ = built_index
    idx = Index(out)
    assert titles(idx.search("what does the thinkery do"))[0] == "Thinkery"
    assert titles(idx.search("thinkry"))[0] == "Thinkery"  # misspelt
    assert titles(idx.search("hansel"))[0] == "Hanzel"  # sounds alike
    assert titles(idx.search("brain barns"))[0] == "Thinkery"  # Civilopedia search term, plural
    assert titles(idx.search("moon gardens"))[0] == "Moon Garden"


def test_a_vowel_heard_before_mb_still_finds_the_name(built_index):
    out, _ = built_index
    # "Embolar" is how speech recognition might write "Mbolo"; without the leading vowel it is closer to
    # Mbolo than to Ambrel, a name that really starts with a vowel.
    assert titles(Index(out).search("what does an embolar do"))[:2] == ["Mbolo", "Ambrel"]


def test_a_district_beats_a_moment_with_the_same_name(built_index):
    out, _ = built_index
    hits = Index(out).search("thinkery finished")
    assert "First Thinkery Finished" in titles(hits)
    hits = Index(out).search("where does a thinkery go")
    assert titles(hits)[0] == "Thinkery"


def test_placement_questions_bring_the_general_rules(built_index):
    out, _ = built_index
    hits = Index(out).search("where can I build a thinkery")
    assert titles(hits)[0] == "Thinkery"
    assert titles(hits)[-1] == "Districts"
    assert "within 2 tiles" in entry_text(hits[-1].page)


def test_index_goes_stale_when_the_game_writes_new_files(built_index, fake_game):
    out, _ = built_index
    assert not index.is_stale(out, fake_game)
    stamp = os.stat(fake_game.modding_log).st_mtime + 60
    os.utime(fake_game.modding_log, (stamp, stamp))
    assert index.is_stale(out, fake_game)


def test_meta_records_mods_and_load_time(built_index):
    out, _ = built_index
    meta = index.read_meta(out)
    assert json.loads(meta["mods"]) == ["Test Balance Mod", "Test Pack"]
    assert float(meta["game_loaded_at"]) > 0


def test_entry_text_stays_within_budget(built_index):
    out, _ = built_index
    page = Index(out).search("thinkery")[0].page
    page["chapters"].append({"title": "Long", "paragraphs": ["word " * 2000]})
    assert len(entry_text(page, budget=1200)) < 1400


def test_soundalike_keys():
    assert phrase_key("Kongo") == phrase_key("Congo")
    assert phrase_key("hanza") == phrase_key("Hansa")
    assert phrase_key("zigurat") == phrase_key("Ziggurat")
    assert phrase_key("harbour") == phrase_key("harbor")
    assert phrase_key("Mbanza") != phrase_key("Hansa")
