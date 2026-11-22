"""Short spoken-style answers from Claude, grounded only in the matched Civilopedia entries.

Players need no key: their questions go to Civspeak's answer service (online.py, answer_service.py),
which holds the key and the limits. A player who adds their own key (Settings > Advanced) is answered
with it directly, and pays for it under their own monthly limit (spend.py). The answer service runs
this same Answerer with its own key and its own ledger.

A player's key comes only from their key store (keys.py: Windows Credential Manager in the Windows
app, the anthropic-key file elsewhere), never from the environment, so no other key on this PC can be
used by accident. It is never logged or printed.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import keys, paths, spend
from .keys import KeyMissing
from .search import Hit, entry_text

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"  # the best answers: the WSL route's model, and the "best" choice


@dataclass(frozen=True)
class Choice:
    """A model the player can pick in Settings ("Answers")."""

    model: str
    label: str
    fallback: bool  # whether Claude may hand a declined question to another model (server-side fallback)


# Haiku 5.5 has no server-side fallback, so a question it declines is simply not answered.
CHOICES = {
    "quick": Choice("claude-haiku-5-5", "Quick", fallback=False),
    "balanced": Choice("claude-sonnet-5-5", "Balanced", fallback=True),
    "best": Choice(MODEL, "Best", fallback=True),
}
APP_DEFAULT_CHOICE = "quick"  # the Windows app's players: cheap and fast (decided 23 Oct 2026)
DEFAULT_CHOICE = "best"  # WSL, the captain's own route, as it always was


def default_choice() -> str:
    return APP_DEFAULT_CHOICE if paths.app_dir() is not None else DEFAULT_CHOICE


def configured_choice() -> str:
    """The "Answers" choice from Settings (CIVSPEAK_ANSWERS overrides it), else this route's default."""
    from . import settings

    chosen = os.environ.get("CIVSPEAK_ANSWERS") or settings.load().answer_model
    return chosen if chosen in CHOICES else default_choice()


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


class AnswerError(RuntimeError):
    pass


def read_key(path: Path | None = None) -> str:
    """The key from a key file (by default civspeak's own anthropic-key file)."""
    return keys.read_key_file(path or paths.key_file())


def build_user_message(question: str, hits: list[Hit], spoken: bool = False) -> str:
    return build_message(question, [entry_text(hit.page) for hit in hits], spoken)


def build_message(question: str, entries: list[str], spoken: bool = False) -> str:
    """The request after the fixed instructions: the numbered entries' text, then the question."""
    blocks = []
    for n, text in enumerate(entries, 1):
        blocks.append(f'<entry number="{n}">\n{text}\n</entry>')
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
    def __init__(
        self,
        ledger: spend.Ledger | None = None,
        key_path: Path | None = None,
        key_store=None,
        choice: str = "",
        service=None,
    ):
        self.ledger = ledger or spend.Ledger(paths.spend_file())
        self.choice = choice  # "" follows Settings, read again for every question
        self.keys = key_store or keys.store(key_path)
        # Where questions go without a key of the player's own: None means Civspeak's answer service
        # (online.AnswerService), False means nowhere (KeyMissing, as the answer service itself runs).
        if service is None:
            from . import online

            service = online.AnswerService()
        self.service = service or None
        self._client = None
        self._client_key_hash = None

    def own_key(self) -> bool:
        """Whether the player added their own key (Settings > Advanced): then it answers, not the service."""
        try:
            return self.keys.present()
        except OSError:
            return False

    def wake(self) -> None:
        """A question is coming (the talk key or the microphone was pressed): wake the answer service."""
        if self.service is not None and not self.own_key():
            self.service.warm_up()

    def warm_up(self) -> None:
        """Import the client library and set up the client now (about a second), not on the first question."""
        if self.service is not None and not self.own_key():
            self.service.warm_up()
            return
        try:
            self._client_for(self.keys.read())
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

        Raises KeyMissing, spend.CapReached, online.LimitReached or AnswerError before anything is spent.
        """
        entries = [entry_text(hit.page) for hit in hits]
        if self.service is not None and not self.own_key():
            yield from self.service.stream(question, entries, spoken)
            return
        yield from self.stream_entries(question, entries, spoken)

    def stream_entries(
        self, question: str, entries: list[str], spoken: bool = False, ledger=None
    ) -> Iterator[tuple[str, object]]:
        """The answer from Claude with this Answerer's key: as stream(), for entries already written
        out as text. `ledger` (default this Answerer's) reserves and settles the spend."""
        ledger = ledger if ledger is not None else self.ledger
        key = self.keys.read()  # before importing Claude's library (a second or more): no key says so at once
        import anthropic

        choice = CHOICES[self.choice if self.choice in CHOICES else configured_choice()]
        user = build_message(question, entries, spoken)
        reserve = spend.worst_case_usd(
            estimate_tokens(SYSTEM_PROMPT + user), MAX_TOKENS, None if choice.fallback else choice.model
        )
        reservation = ledger.reserve(reserve)
        client = self._client_for(key)
        speakable = SpokenFilter()
        # A declined question goes on to another model where Claude offers that (Opus and Sonnet).
        fallback = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if choice.fallback else {}
        try:
            with client.beta.messages.stream(
                model=choice.model,
                max_tokens=MAX_TOKENS,
                **fallback,
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
            raise AnswerError(_friendly_error(e, status, self.keys.where)) from None

        cost, usage = _cost(final, choice.model)
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
        yield "done", Result(answer, [u for u in used if 1 <= u <= len(entries)], cost, total, final.model, usage, refused)


def _cost(message, asked: str = MODEL) -> tuple[float, dict]:
    usage = message.usage
    iterations = getattr(usage, "iterations", None) or []
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    cost = 0.0
    entries = iterations or [usage]
    for it in entries:
        served_by_fallback = getattr(it, "type", "") == "fallback_message"
        model = message.model if served_by_fallback else asked
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
            for m in {model, message.model, asked}
        )
    return cost, totals


def _friendly_error(e, status, where: str) -> str:
    if status == 401:
        if where == keys.CredentialKeys.where:
            return "The answer key was not accepted. Add it again in Civspeak's Settings."
        return f"The answer key was rejected. Check the key in {where}."
    if status == 403:
        return "The answer key is not allowed to use this model."
    if status == 429:
        return "Too many questions at once right now. Try again in a moment."
    if status and status >= 500:
        return "Answers are having trouble right now. Try again in a moment."
    if status:
        return f"Could not get an answer (error {status})."
    return "Could not get an answer. Check the internet connection."
