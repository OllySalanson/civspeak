"""Read the game's Modding.log to learn exactly which text files the last loaded game used.

The log is the ground truth for what the game showed: it lists, for the last game that was
set up, every enabled mod component and every text file it loaded, in load order, and marks
files that failed to load. Replaying that list reproduces the game's text without
re-implementing Firaxis's mod criteria rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_STAMP = re.compile(r"^\[\d+(?:\.\d+)?\]\s?")
_MOD_HEADER = re.compile(r"^([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}) \((.*)\)$")
_COMPONENT = re.compile(r"^ \* (.+) \((\w+)\)$")
_APPLYING = re.compile(r"^Applying Component - (.+) \((\w+)\)$")
_TEXT_LOAD = re.compile(r"^LocalizedText - Loading (.+)$")

APPLY_START = "Modding Framework - Applying Components"
APPLY_END = "Modding Framework - Finished Apply Components"
TARGET_LIST = "Target in-game actions (in order of application):"
TEXT_FAILED = "Warning: LocalizedText - Failed loading XML."


@dataclass
class EnabledComponent:
    mod_id: str
    mod_name: str
    component_id: str
    component_type: str


@dataclass
class AppliedText:
    component_id: str
    component_type: str
    files: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class GameSession:
    """The last in-game mod setup recorded in Modding.log."""

    components: list[EnabledComponent]
    texts: list[AppliedText]

    @property
    def mods(self) -> dict[str, str]:
        """mod id -> mod name, in first-seen order."""
        out: dict[str, str] = {}
        for c in self.components:
            out.setdefault(c.mod_id.lower(), c.mod_name)
        return out


class LogError(RuntimeError):
    pass


def _lines(text: str) -> list[str]:
    return [_STAMP.sub("", line.rstrip("\r\n")) for line in text.splitlines()]


def parse(text: str) -> GameSession:
    lines = _lines(text)

    start = end = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] == APPLY_END and end is None:
            end = i
        elif lines[i] == APPLY_START and end is not None:
            start = i
            break
    if start is None or end is None:
        raise LogError("Modding.log has no finished in-game setup yet. Start or load a game in Civ VI once, then try again.")

    target = None
    for i in range(start, -1, -1):
        if lines[i] == TARGET_LIST:
            target = i
            break

    components: list[EnabledComponent] = []
    if target is not None:
        mod_id = mod_name = None
        for line in lines[target + 1 : start]:
            m = _MOD_HEADER.match(line)
            if m:
                mod_id, mod_name = m.group(1), m.group(2)
                continue
            c = _COMPONENT.match(line)
            if c and mod_id:
                components.append(EnabledComponent(mod_id, mod_name or "", c.group(1), c.group(2)))
                continue
            if line.startswith(" * "):
                continue  # "Unknown Component" and similar
            break

    texts: list[AppliedText] = []
    current: AppliedText | None = None
    last_file: str | None = None
    for line in lines[start + 1 : end]:
        a = _APPLYING.match(line)
        if a:
            current = AppliedText(a.group(1), a.group(2))
            texts.append(current)
            last_file = None
            continue
        t = _TEXT_LOAD.match(line)
        if t and current is not None:
            last_file = t.group(1).strip()
            current.files.append(last_file)
            continue
        if line == TEXT_FAILED and current is not None and last_file is not None:
            if last_file in current.files:
                current.files.remove(last_file)
                current.failed.append(last_file)
            last_file = None

    return GameSession(components=components, texts=[t for t in texts if t.files or t.failed])
