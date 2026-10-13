"""A Python take on the game's Locale.Lookup: tag lookup, placeholder fill and markup cleanup.

Game text uses placeholders such as {1_Num}, {1_Num : plural 1?district; other?districts;},
{1: number +#;-#}, nested tags such as {LOC_DISTRICT_CAMPUS_NAME}, and markup such as
[ICON_Science], [NEWLINE] and [COLOR_Red]...[ENDCOLOR].
"""

from __future__ import annotations

import re

MISSING = "[value]"
_MAX_DEPTH = 8

_TAG = re.compile(r"^LOC_[A-Za-z0-9_]+$")
_ARG = re.compile(r"^(\d+)(?:_\w*)?$")


class Localizer:
    def __init__(self, tags: dict[str, str]):
        self.tags = tags

    def has(self, tag: str | None) -> bool:
        return bool(tag) and tag in self.tags

    def lookup(self, key, *args) -> str:
        """Like Locale.Lookup: resolve a tag (or take literal text) and fill its placeholders."""
        return self._lookup(key, args, 0)

    def _lookup(self, key, args, depth: int) -> str:
        if key is None:
            return ""
        if not isinstance(key, str):
            return _number(key)
        text = self.tags.get(key)
        if text is None:
            if _TAG.match(key):
                return _humanize_tag(key)
            text = key
        return self._format(text, args, depth)

    def _arg(self, value, depth: int) -> str:
        if isinstance(value, str) and value in self.tags and depth < _MAX_DEPTH:
            return self._lookup(value, (), depth + 1)
        if isinstance(value, str):
            return value
        return _number(value)

    def _format(self, text: str, args, depth: int) -> str:
        if "{" not in text:
            return text
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch != "{":
                j = text.find("{", i)
                if j == -1:
                    out.append(text[i:])
                    break
                out.append(text[i:j])
                i = j
                continue
            end = _matching_brace(text, i)
            if end == -1:
                out.append(text[i:])
                break
            out.append(self._placeholder(text[i + 1 : end], args, depth))
            i = end + 1
        return "".join(out)

    def _placeholder(self, body: str, args, depth: int) -> str:
        body = body.strip()
        # Grammar forms like {LOC_X << {1_CivName}} - render the inner value.
        if "<<" in body:
            left, _, right = body.partition("<<")
            right = right.strip()
            if right.startswith("{") and right.endswith("}"):
                return self._placeholder(right[1:-1], args, depth)
            return self._placeholder(left, args, depth)
        name, sep, spec = body.partition(":")
        name = name.strip()
        if _TAG.match(name):
            if depth >= _MAX_DEPTH:
                return ""
            return self._lookup(name, (), depth + 1)
        m = _ARG.match(name)
        value = None
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(args):
                value = args[idx]
        if value is None:
            if sep and spec.strip().startswith("plural"):
                # Unknown count: prefer the "other" (plural) wording.
                return self._format(_plural_choice(spec, None), args, depth + 1)
            return MISSING
        spec = spec.strip()
        if spec.startswith("plural"):
            return self._format(_plural_choice(spec, value), args, depth + 1)
        if spec.startswith("number"):
            return _format_number(value, spec[len("number") :].strip())
        return self._arg(value, depth)


def _matching_brace(text: str, start: int) -> int:
    level = 0
    for k in range(start, len(text)):
        if text[k] == "{":
            level += 1
        elif text[k] == "}":
            level -= 1
            if level == 0:
                return k
    return -1


def _to_number(value) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _number(value) -> str:
    num = _to_number(value)
    if num is None:
        return str(value)
    if num == int(num):
        return str(int(num))
    return f"{num:g}"


def _plural_choice(spec: str, value) -> str:
    # spec: "plural 1?district; other?districts;"
    options = {}
    for part in spec[len("plural") :].split(";"):
        if "?" in part:
            k, _, v = part.partition("?")
            options[k.strip()] = v
    num = _to_number(value) if value is not None else None
    if num is not None and num == 1 and "1" in options:
        return options["1"]
    if num is not None and _number(num) in options:
        return options[_number(num)]
    return options.get("other", next(iter(options.values()), ""))


def _format_number(value, pattern: str) -> str:
    num = _to_number(value)
    if num is None:
        return str(value)
    if not pattern:
        return _number(num)
    sections = pattern.split(";")
    if num < 0 and len(sections) > 1:
        fmt = sections[1]
        num = -num
    else:
        fmt = sections[0]
    prefix = re.match(r"^[^#0,.]*", fmt).group(0)
    suffix = re.search(r"[^#0,.]*$", fmt).group(0)
    core = fmt[len(prefix) : len(fmt) - len(suffix)] if suffix else fmt[len(prefix) :]
    decimals = len(core.split(".", 1)[1]) if "." in core else 0
    grouping = "," in core
    text = f"{num:,.{decimals}f}" if grouping else f"{num:.{decimals}f}"
    if decimals and "#" in core.split(".", 1)[1]:
        text = text.rstrip("0").rstrip(".")
    return f"{prefix}{text}{suffix}"


def _humanize_tag(tag: str) -> str:
    """Last resort for a tag with no text: LOC_DISTRICT_CAMPUS_NAME -> 'District Campus'."""
    words = tag[4:].split("_")
    while words and words[-1] in ("NAME", "DESCRIPTION", "TITLE", "SHORT"):
        words.pop()
    return " ".join(w.capitalize() for w in words)


# -- markup cleanup -------------------------------------------------------------------------

# Spoken/plain names for icons when the word is not already written next to the icon.
ICON_WORDS = {
    "strength": "Combat Strength",
    "rangedstrength": "Ranged Strength",
    "ranged": "Ranged Strength",
    "bombard": "Bombard Strength",
    "religiousstrength": "Religious Strength",
    "movement": "Movement",
    "range": "Range",
    "gold": "Gold",
    "faith": "Faith",
    "science": "Science",
    "culture": "Culture",
    "food": "Food",
    "production": "Production",
    "housing": "Housing",
    "amenities": "Amenities",
    "citizen": "Citizen",
    "tourism": "Tourism",
    "favor": "Diplomatic Favor",
    "power": "Power",
    "greatwork": "Great Work",
    "traderoute": "Trade Route",
    "envoy": "Envoy",
    "governor": "Governor",
    "charges": "charges",
    "turn": "turns",
    "turns": "turns",
    "greatperson": "Great Person points",
    "religion": "Religion",
    "capital": "Capital",
    "army": "Army",
    "corps": "Corps",
    "district": "District",
    "health": "Health",
    "damaged": "Damaged",
    "pressure": "Religious Pressure",
    "loyalty": "Loyalty",
    "co2": "CO2",
    "glory": "Glory",
}

_COLOR = re.compile(r"\[(?:COLOR[_:][^\]]*|ENDCOLOR|SIZE[_:][^\]]*|STYLE[_:][^\]]*|ENDSTYLE|[iIbBuU]|/[iIbBuU])\]")
_ICON = re.compile(r"\[(?:ICON|Icon|icon)_([A-Za-z0-9_]+)\]\s*")
_NEWLINE = re.compile(r"\[(?:NEWLINE|newline|NewLine)\]")
_SPACES = re.compile(r"[ \t]+")


def _icon_word(name: str) -> str:
    key = name.lower()
    if key == "bullet":
        return "• "
    if key.startswith("resource_"):
        return name[len("resource_") :].replace("_", " ").title()
    for prefix in ("greatwork_", "great_work_"):
        if key.startswith(prefix):
            return "Great Work"
    if key.startswith("greatperson") or key.startswith("great_person"):
        return "Great Person points"
    word = ICON_WORDS.get(key.replace("_", ""))
    if word is None:
        if key.startswith(("leader_", "civilization_", "unit_", "building_", "district_", "improvement_")):
            return ""
        word = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ").strip()
        word = " ".join(w if w.isupper() and len(w) > 1 else w.capitalize() for w in word.split())
    return word


def clean(text: str) -> str:
    """Game markup -> plain text with newlines; icons become words unless already written."""
    if not text:
        return ""
    text = _NEWLINE.sub("\n", text)
    text = _COLOR.sub("", text)

    def icon(m: re.Match) -> str:
        word = _icon_word(m.group(1))
        if word in ("", "• "):
            return word
        following = text[m.end() : m.end() + 40].lower()
        last = word.split()[-1].lower()
        compact = word.replace(" ", "").lower()
        if (
            following.startswith(word.lower())
            or following.startswith(last)
            or following.replace(" ", "").startswith(compact)
            or (last.endswith("s") and following.startswith(last[:-1]))
        ):
            return ""  # "+2 [ICON_Science] Science" -> "+2 Science"
        return word + " "

    text = _ICON.sub(icon, text)
    text = _SPACES.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
