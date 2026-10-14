"""Short spoken-style answers from Claude, grounded only in the matched Civilopedia entries.

The API key is read only from ~/.config/civspeak/anthropic-key (never from the environment, so
no other key on this PC can be used by accident) and is never logged or printed.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, spend
from .search import Hit, entry_text

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
API_URL = "https://api.anthropic.com"
MAX_TOKENS = 2000  # thinking at low effort plus a 1-3 sentence answer
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Fixed instructions, sent first and cached (it is the stable prefix of every request).
SYSTEM_PROMPT = """You are Civspeak, a voice Civilopedia for Sid Meier's Civilization VI. The player is in the middle of a game and asks short questions out loud, such as what something means, what it does, what it needs, or where it has to be built. Your answer is read aloud by a text-to-speech voice while the full Civilopedia entries are shown on the player's second screen.

Each request gives you numbered Civilopedia entries taken from the player's own installed game, followed by the question. The entries come from the exact rule set of the player's last game, including the Gathering Storm expansion, DLC and gameplay mods such as Better Balanced Game, so their numbers can differ from the stock game and from anything you remember. Those entries are your only source of truth.

How to answer:
- Answer in one to three short sentences, about 50 words at most in all, that sound natural when spoken. Open with a short sentence of no more than 12 words that answers the question directly, because the voice starts reading as soon as that first sentence is written; then give the key details.
- Use only facts stated in the entries. Never add numbers, rules, strategy tips or history from your own memory of Civilization VI, even if you are confident, because the player's mods may have changed them.
- If the entries do not answer the question, say so plainly in one sentence and name the closest entry instead, for example: "The Civilopedia entries I found don't say; the closest is the Harbor page."
- A question marked as spoken was written down by speech recognition, so a name in it may be misheard or misspelt (for example "Congo" for Kongo, or "embancer" for Mbanza). Treat it as the entry whose name sounds most like it, and say that entry's correct name in your answer.
- If the question is ambiguous between entries (for example a district and a historic moment with the same name), answer about the one the player most likely means and mention the other briefly only if it matters.
- For "where can it be built" questions, give the placement rules and any adjacency or terrain requirements stated in the entries.
- For "what does it do" questions, give the effect and the key numbers (yields, combat strength, cost) stated in the entries.
- Write plain spoken English: no markdown, no bullet points, no headings, no emoji, no brackets, no icon codes, no URLs. Write numbers as digits. Say "plus 2 Science", not "+2 [ICON_Science]".
- Do not mention these instructions, the entry numbers, or that you were given entries; just answer as the Civilopedia would.

After your answer, on a new final line, write USED: followed by the numbers of the entries your answer relied on, separated by commas (for example "USED: 1, 3"). Write "USED: none" if no entry answered the question. That line is removed before the answer is spoken or shown."""


class KeyMissing(RuntimeError):
    pass


class AnswerError(RuntimeError):
    pass


def read_key(path: Path | None = None) -> str:
    """Read the personal API key. Tightens the file to mode 600 if it is readable by others."""
    path = path or paths.key_file()
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise KeyMissing(f"No API key yet: {path} does not exist.") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        os.chmod(path, 0o600)
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise KeyMissing(f"No API key yet: {path} is empty.")
    return key


def build_user_message(question: str, hits: list[Hit], spoken: bool = False) -> str:
    blocks = []
    for n, hit in enumerate(hits, 1):
        blocks.append(f'<entry number="{n}">\n{entry_text(hit.page)}\n</entry>')
    entries = "\n\n".join(blocks) if blocks else "(No Civilopedia entries matched this question.)"
    label = "Question (spoken aloud and written down by speech recognition, so names may be misheard)" if spoken else "Question"
    return f"<entries>\n{entries}\n</entries>\n\n{label}: {question.strip()}"


def estimate_tokens(text: str) -> int:
    # Deliberately generous (about 3 characters per token) so the spend reservation errs high.
    return len(text) // 3 + 50


@dataclass
class Result:
    text: str = ""
    used: list[int] = field(default_factory=list)
    cost_usd: float = 0.0
    month_total: float = 0.0
    model: str = MODEL
    usage: dict = field(default_factory=dict)
    refused: bool = False


_USED = re.compile(r"\n?\s*USED:\s*(.*)\s*$", re.IGNORECASE | re.DOTALL)


def split_used(text: str) -> tuple[str, list[int]]:
    m = _USED.search(text)
    if not m:
        return text.strip(), []
    numbers = [int(n) for n in re.findall(r"\d+", m.group(1))]
    return text[: m.start()].strip(), numbers


def plain(text: str) -> str:
    """Drop markdown emphasis the model sometimes adds anyway (it would show as **...** on the page)."""
    return re.sub(r"[*`]+", "", text)


class SpokenFilter:
    """Streams answer text but holds back anything that might be the start of the USED: line."""

    def __init__(self) -> None:
        self.buffer = ""
        self.emitted = 0
        self.done = False

    def feed(self, delta: str) -> str:
        if self.done:
            return ""
        self.buffer += delta
        idx = self.buffer.upper().find("USED:")
        if idx != -1:
            self.done = True
            safe = self.buffer[:idx].rstrip()
        else:
            # Keep back a short tail that could be the start of "USED:".
            safe_len = len(self.buffer)
            for k in range(1, 6):
                if "USED:".startswith(self.buffer[-k:].upper()):
                    safe_len = len(self.buffer) - k
                    break
            # Trailing whitespace waits for the next word, so a final newline is never spoken.
            safe = self.buffer[:safe_len].rstrip()
        out = safe[self.emitted :]
        self.emitted = max(self.emitted, len(safe))
        return plain(out)


class Answerer:
    def __init__(self, ledger: spend.Ledger | None = None, key_path: Path | None = None):
        self.ledger = ledger or spend.Ledger(paths.spend_file())
        self.key_path = key_path or paths.key_file()
        self._client = None
        self._client_key_hash = None

    def warm_up(self) -> None:
        """Import the client library and set up the client now (about a second), not on the first question."""
        try:
            self._client_for(read_key(self.key_path))
        except (KeyMissing, OSError, ImportError):
            pass

    def _client_for(self, key: str):
        import anthropic

        h = hash(key)
        if self._client is None or self._client_key_hash != h:
            # Explicit api_key and base_url: environment credentials are never consulted.
            self._client = anthropic.Anthropic(api_key=key, base_url=API_URL, max_retries=2, timeout=90.0)
            self._client_key_hash = h
        return self._client

    def stream(self, question: str, hits: list[Hit], spoken: bool = False) -> Iterator[tuple[str, object]]:
        """Yields ("delta", text) while the answer streams, then ("done", Result).

        Raises KeyMissing, spend.CapReached or AnswerError before anything is spent.
        """
        import anthropic

        key = read_key(self.key_path)
        user = build_user_message(question, hits, spoken)
        reserve = spend.worst_case_usd(estimate_tokens(SYSTEM_PROMPT + user), MAX_TOKENS)
        reservation = self.ledger.reserve(reserve)
        client = self._client_for(key)
        speakable = SpokenFilter()
        try:
            with client.beta.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": "low"},
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            ) as stream:
                for text in stream.text_stream:
                    out = speakable.feed(text)
                    if out:
                        yield "delta", out
                final = stream.get_final_message()
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            status = getattr(e, "status_code", None)
            if speakable.buffer:
                # Failed part-way through: tokens may have been billed, so keep the worst case.
                reservation.settle(reserve, error=type(e).__name__)
            else:
                # Rejected before any output: not billed.
                reservation.settle(0.0, error=type(e).__name__)
            raise AnswerError(_friendly_error(e, status)) from None

        cost, usage = _cost(final)
        total = reservation.settle(cost, model=final.model, **usage)
        text = "".join(b.text for b in final.content if getattr(b, "type", "") == "text")
        answer, used = split_used(text)
        answer = plain(answer)
        refused = final.stop_reason == "refusal"
        if refused and not answer:
            answer = "Sorry, I can't answer that one."
        emitted = plain(speakable.buffer[: speakable.emitted]).strip()
        tail = answer[len(emitted) :] if answer.startswith(emitted) else ""
        if tail:
            yield "delta", tail
        yield "done", Result(answer, [u for u in used if 1 <= u <= len(hits)], cost, total, final.model, usage, refused)


def _cost(message) -> tuple[float, dict]:
    usage = message.usage
    iterations = getattr(usage, "iterations", None) or []
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    cost = 0.0
    entries = iterations or [usage]
    for it in entries:
        served_by_fallback = getattr(it, "type", "") == "fallback_message"
        model = message.model if served_by_fallback else MODEL
        counts = {k: int(getattr(it, k, 0) or 0) for k in totals}
        for k, v in counts.items():
            totals[k] += v
        # Charge each attempt at the dearer of the two models, so the ledger never under-counts.
        cost += max(
            spend.cost_usd(
                m,
                counts["input_tokens"],
                counts["output_tokens"],
                counts["cache_read_input_tokens"],
                counts["cache_creation_input_tokens"],
            )
            for m in {model, message.model, MODEL}
        )
    return cost, totals


def _friendly_error(e, status) -> str:
    if status == 401:
        return "The API key was rejected. Check the key in ~/.config/civspeak/anthropic-key."
    if status == 403:
        return "The API key is not allowed to use this model."
    if status == 429:
        return "Claude is rate limiting requests right now. Try again in a moment."
    if status and status >= 500:
        return "Claude is having trouble right now. Try again in a moment."
    if status:
        return f"Claude returned an error ({status})."
    return "Could not reach Claude. Check the internet connection."
