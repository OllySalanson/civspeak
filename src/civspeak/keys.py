"""The player's own Claude API key: where it is kept, and checking a new one without spending anything.

Under Windows' own Python (the Windows app) the key lives in Windows Credential Manager, tied to the
player's Windows login and encrypted by Windows; a key file left by an earlier Civspeak is moved in
there and deleted. Elsewhere (WSL) it is the anthropic-key file in civspeak's config folder, as before.
The key never comes from the environment, and it is never printed, logged or sent back to the page.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

from . import paths, system

log = logging.getLogger(__name__)

CREDENTIAL = "Civspeak/Claude API key"
CONSOLE_KEYS_URL = "https://console.anthropic.com/settings/keys"
_KEY_SHAPE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,300}$")


class KeyMissing(RuntimeError):
    pass


def read_key_file(path: Path) -> str:
    """Read a key file. Tightens it to mode 600 if it is readable by others (Linux only: Windows
    reports every file as 666, and the user's own folder already keeps other users out)."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise KeyMissing(f"No API key yet: {path} does not exist.") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO) and os.name != "nt":
        os.chmod(path, 0o600)
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise KeyMissing(f"No API key yet: {path} is empty.")
    return key


class FileKeys:
    """The key in a file of its own (mode 600), as in WSL."""

    def __init__(self, path: Path):
        self.path = path
        self.where = str(path)

    def read(self) -> str:
        return read_key_file(self.path)

    def present(self) -> bool:
        try:
            return self.path.stat().st_size > 0
        except OSError:
            return False

    def save(self, key: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class CredentialKeys:
    """The key in Windows Credential Manager. `api` is (read, write, delete) for tests."""

    where = "Windows Credential Manager"

    def __init__(self, legacy: Path | None = None, target: str = CREDENTIAL, api=None):
        self.target = target
        self.legacy = legacy
        self._read, self._write, self._delete = api or (system.credential_read, system.credential_write, system.credential_delete)

    def read(self) -> str:
        try:
            key = self._read(self.target)
        except OSError as e:
            raise KeyMissing(f"The Claude key could not be read from Windows Credential Manager ({e}).") from None
        if key:
            return key
        if self.legacy is not None and self.legacy.exists():
            key = read_key_file(self.legacy)
            self.save(key)  # move it somewhere safer
            self.legacy.unlink(missing_ok=True)
            log.info("moved the Claude key from %s into Windows Credential Manager", self.legacy)
            return key
        raise KeyMissing("No Claude key yet: add one in Civspeak's Settings.")

    def present(self) -> bool:
        try:
            return bool(self.read())
        except KeyMissing:
            return False

    def save(self, key: str) -> None:
        self._write(self.target, key, "Claude API key")

    def delete(self) -> None:
        self._delete(self.target)
        if self.legacy is not None:
            self.legacy.unlink(missing_ok=True)


def store(path: Path | None = None) -> FileKeys | CredentialKeys:
    """Where this Civspeak keeps the key: a given file, Credential Manager in the Windows app, or the
    usual key file elsewhere."""
    if path is not None:
        return FileKeys(path)
    if system.IS_WINDOWS:
        return CredentialKeys(legacy=paths.key_file())
    return FileKeys(paths.key_file())


def tidy(text: str) -> str:
    """A pasted key without the spaces, quotes or line breaks that come along when copying it."""
    return re.sub(r"\s+", "", text).strip("\"'")


def shape_problem(key: str) -> str:
    """Why `key` cannot be a Claude API key, or "" if it looks like one."""
    if not key:
        return "Paste your Claude API key first."
    if not key.startswith("sk-ant-"):
        return "That isn't a Claude API key: those start with sk-ant-. Copy it again from the Claude Console."
    if not _KEY_SHAPE.match(key):
        return "That key looks cut short or has extra characters in it. Copy it again from the Claude Console."
    return ""


def check(key: str, model: str, client_for=None) -> str:
    """Whether Claude accepts `key` and lets it use `model`, asked in a way that costs nothing (the
    Models API); "" if it does, otherwise what is wrong in the player's words."""
    problem = shape_problem(key)
    if problem:
        return problem
    import anthropic

    from .answer import API_URL

    client = (client_for or (lambda k: anthropic.Anthropic(api_key=k, base_url=API_URL, max_retries=1, timeout=20.0)))(key)
    try:
        client.models.retrieve(model)
    except anthropic.AuthenticationError:
        return "Claude did not accept that key. Check you copied all of it, or create a new one in the Claude Console."
    except anthropic.PermissionDeniedError:
        return "That key is not allowed to use Claude's models. Check the key's workspace in the Claude Console."
    except anthropic.NotFoundError:
        return f"That key works, but it cannot use {model}. Check the key's workspace in the Claude Console."
    except anthropic.APIConnectionError:
        return "Could not reach Claude to check the key. Check the internet connection and try again."
    except anthropic.APIStatusError as e:
        return f"Claude could not check the key just now (error {e.status_code}). Try again in a moment."
    return ""
