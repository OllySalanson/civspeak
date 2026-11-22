"""What the player chose in Settings: the monthly spending limit, the Civ VI folder (when Civspeak could
not find it by itself) and whether the first-run screen is done. Kept as settings.json in civspeak's
config folder; anything missing or unreadable falls back to its default. The key is not here (keys.py).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

FILE_NAME = "settings.json"
MAX_CAP_USD = 1000.0
_lock = threading.RLock()


@dataclass
class Settings:
    cap_usd: float | None = None  # None: the default (spend.default_cap)
    game_dir: str = ""  # the Civ VI install chosen by hand; "" means found by itself
    setup_done: bool = False  # the first-run screen was finished (or skipped)
    answer_model: str = ""  # "quick", "balanced" or "best" (answer.CHOICES); "" means this route's default


def path() -> Path:
    return paths.config_dir() / FILE_NAME


def load() -> Settings:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    out = Settings()
    for f in fields(Settings):
        if f.name in raw:
            setattr(out, f.name, raw[f.name])
    # Throw away anything of the wrong kind rather than trust it.
    if not isinstance(out.cap_usd, (int, float)) or isinstance(out.cap_usd, bool) or not 0 < out.cap_usd <= MAX_CAP_USD:
        out.cap_usd = None
    if not isinstance(out.game_dir, str):
        out.game_dir = ""
    out.setup_done = out.setup_done is True
    if not isinstance(out.answer_model, str):
        out.answer_model = ""
    return out


def save(settings: Settings) -> None:
    """Write the settings (a whole new file, swapped in: settings.json is never held open)."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".new")
    with _lock:
        temp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
        os.replace(temp, target)


def update(**changes) -> Settings:
    """Change some settings and keep the rest."""
    with _lock:
        current = load()
        for name, value in changes.items():
            if name not in {f.name for f in fields(Settings)}:
                raise KeyError(name)
            setattr(current, name, value)
        save(current)
    return current
