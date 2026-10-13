"""Find mod folders (DLC, Steam Workshop, local Mods) and resolve the text files the log names."""

from __future__ import annotations

import glob
import os
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .modlog import GameSession


@dataclass
class ModInfo:
    mod_id: str
    folder: Path
    name: str = ""


@dataclass
class ResolvedText:
    path: Path
    mod_id: str
    component_id: str


@dataclass
class TextPlan:
    files: list[ResolvedText] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    failed_in_game: list[str] = field(default_factory=list)


def scan_modinfos(roots: list[Path]) -> dict[str, list[ModInfo]]:
    """mod id (lowercase) -> mods with that id. Only looks one or two folders deep."""
    found: dict[str, list[ModInfo]] = defaultdict(list)
    for root in roots:
        if not root.is_dir():
            continue
        patterns = [
            os.path.join(glob.escape(str(root)), "*", "*.modinfo"),
            os.path.join(glob.escape(str(root)), "*", "*", "*.modinfo"),
        ]
        for pattern in patterns:
            for path in sorted(glob.glob(pattern)):
                info = _read_modinfo(Path(path))
                if info:
                    found[info.mod_id.lower()].append(info)
    return found


def _read_modinfo(path: Path) -> ModInfo | None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    mod_id = root.get("id")
    if root.tag != "Mod" or not mod_id:
        return None
    name = root.findtext("Properties/Name") or ""
    return ModInfo(mod_id=mod_id, folder=path.parent, name=name.strip())


def resolve_case_insensitive(base: Path, rel: str) -> Path | None:
    """Join base and a game-style relative path, matching names case-insensitively like Windows."""
    path = base / rel.replace("\\", "/")
    if path.is_file():
        return path
    current = base
    for part in Path(rel.replace("\\", "/")).parts:
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        try:
            names = os.listdir(current)
        except OSError:
            return None
        match = next((n for n in names if n.lower() == part.lower()), None)
        if match is None:
            return None
        current = current / match
    return current if current.is_file() else None


def plan_text(session: GameSession, modinfos: dict[str, list[ModInfo]]) -> TextPlan:
    """Map each text file the log loaded to a real file on disk, in the game's load order.

    Component ids are only unique within a mod, so each applied component is matched to the
    enabled (mod, component) entries with the same id and type, preferring entries not yet used
    and whose folder actually holds the files.
    """
    pending: dict[tuple[str, str], list[str]] = defaultdict(list)
    for c in session.components:
        pending[(c.component_id, c.component_type)].append(c.mod_id.lower())

    def best_folder(mod_ids) -> tuple[str, list[Path | None]] | None:
        best = None
        for mod_id in mod_ids:
            for info in modinfos.get(mod_id, []):
                paths = [resolve_case_insensitive(info.folder, f) for f in applied.files]
                hits = sum(p is not None for p in paths)
                if hits and (best is None or hits > best[0]):
                    best = (hits, mod_id, paths)
                if best and hits == len(paths):
                    return best[1], best[2]
        return (best[1], best[2]) if best else None

    plan = TextPlan()
    for applied in session.texts:
        plan.failed_in_game.extend(applied.failed)
        candidates = pending.get((applied.component_id, applied.component_type), [])
        # Fall back to every known mod when the enabled list did not name this component.
        chosen = best_folder(candidates) or best_folder(list(modinfos))
        if chosen is None:
            plan.unresolved.extend(f"{applied.component_id}: {f}" for f in applied.files)
            continue
        mod_id, paths = chosen
        if mod_id in candidates:
            candidates.remove(mod_id)
        for rel, p in zip(applied.files, paths, strict=True):
            if p is None:
                plan.unresolved.append(f"{applied.component_id}: {rel}")
            else:
                plan.files.append(ResolvedText(path=p, mod_id=mod_id, component_id=applied.component_id))
    return plan
