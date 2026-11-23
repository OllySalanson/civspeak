"""The website in site/: every file it points at exists, and its words keep to the rules.

The site is plain files with no build step, so these read the files as they are published.
"""

import re
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "site"
PAGE = SITE / "index.html"
TEXT_FILES = [PAGE, SITE / "css" / "site.css", SITE / "js" / "site.js"]
DOWNLOAD = "https://github.com/OllySalanson/civspeak/releases/latest/download/Civspeak-Setup.exe"
AGES = ["ancient", "classical", "medieval", "renaissance", "industrial", "modern", "atomic", "information"]

# Never on the site: who writes the answers, any limit on asking, small print about the game's makers.
BANNED = [
    r"claude",
    r"anthropic",
    r"openai",
    r"\blimits?\b",
    r"\bcaps?\b",
    r"\bquota",
    r"per day",
    r"a day\b",
    r"firaxis",
    r"affiliated",
    r"fan[- ]made",
    r"unofficial",
    r"dyslex",
]


def test_every_file_the_page_points_at_exists():
    html = PAGE.read_text(encoding="utf-8")
    links = re.findall(r'(?:href|src)="([^"]+)"', html)
    local = [link for link in links if not link.startswith(("http", "#", "mailto:"))]
    assert local, "the page should use its own stylesheet, script and pictures"
    for link in local:
        assert (SITE / link).is_file(), f"{link} is linked from the page but is not in site/"


def test_the_stylesheet_fonts_exist():
    css = (SITE / "css" / "site.css").read_text(encoding="utf-8")
    fonts = re.findall(r"url\(\.\./(fonts/[^)]+)\)", css)
    assert len(fonts) >= 3
    for font in fonts:
        assert (SITE / font).is_file(), f"{font} is used by the stylesheet but is not in site/"
    assert list((SITE / "fonts").glob("OFL-*.txt")), "each font family needs its licence beside it"


def test_download_buttons_fetch_the_latest_installer():
    html = PAGE.read_text(encoding="utf-8")
    assert html.count(DOWNLOAD) >= 3, "the menu bar, the heading and the last section each have a Download"
    assert "releases/tag/" not in html, "link the latest release, never one fixed version"


def test_there_is_a_painting_for_each_of_the_eight_ages():
    script = (SITE / "js" / "site.js").read_text(encoding="utf-8")
    for number, name in enumerate(AGES, start=1):
        picture = f"{number}-{name}"
        assert (SITE / "img" / "ages" / f"{picture}.webp").is_file(), f"no painting for {name}"
        assert f'"{picture}"' in script, f"the script does not show {name}"


def test_the_menu_bar_is_try_it_how_it_works_and_download():
    html = PAGE.read_text(encoding="utf-8")
    bar = html[html.index('<nav class="bar"') : html.index("</nav>")]
    labels = [label.strip() for label in re.findall(r">([^<>]+)</a>", bar) if label.strip()]
    assert labels == ["Try it", "How it works", "Download"]
    assert 'href="#try"' in bar and 'id="try"' in html
    assert 'href="#how"' in bar and 'id="how"' in html


def test_the_words_keep_to_the_rules():
    for path in TEXT_FILES:
        text = path.read_text(encoding="utf-8")
        assert "—" not in text, f"{path.name}: use a plain dash"
        for pattern in BANNED:
            found = re.search(pattern, text, re.IGNORECASE)
            assert not found, f"{path.name} says {found.group(0)!r}, which the site must not mention"


def test_the_main_lines_are_the_ones_chosen():
    html = PAGE.read_text(encoding="utf-8")
    assert "Every age had someone to ask.<br><em>Now you do too.</em>" in html
    assert "Speak to the Civilopedia, and it speaks back." in html
    assert "Civ <i>Speak</i>" in html


def test_the_site_ships_no_sound_files():
    # tools/repo_guard.py refuses recordings; the site's voices are the browser's and its sounds are made in the page.
    sounds = [p.name for p in SITE.rglob("*") if p.suffix.lower() in {".mp3", ".ogg", ".wav", ".webm", ".flac", ".m4a"}]
    assert sounds == []
