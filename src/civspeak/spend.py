"""Local monthly spend tracking with a hard cap.

Before each Claude call the worst-case cost is reserved; the call only goes ahead if this
month's spend plus that reservation stays within the cap. After the call the reservation is
settled with the real cost from the API's usage numbers. A reservation that never settles
(crash, network loss mid-answer) keeps counting at its worst-case value, so the cap can never be
exceeded by accident.

The ledger is an append-only JSON-lines file in ~/.local/share/civspeak/spend.jsonl.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DEFAULT_CAP_USD = 10.0

# USD per million tokens: (input, output, cache read, cache write 5 minutes).
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
}
# Anything unrecognised (for example a fallback model) is charged at a deliberately high rate.
UNKNOWN_PRICE = (10.00, 50.00, 1.00, 12.50)


class CapReached(RuntimeError):
    def __init__(self, spent: float, cap: float):
        super().__init__(f"Monthly limit reached: ${spent:.2f} of ${cap:.2f} spent this month.")
        self.spent = spent
        self.cap = cap


def month_key(t: float | None = None) -> str:
    return time.strftime("%Y-%m", time.localtime(t if t is not None else time.time()))


def price(model: str | None) -> tuple[float, float, float, float]:
    return PRICES.get(model or "", UNKNOWN_PRICE)


def cost_usd(model: str | None, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0) -> float:
    p_in, p_out, p_read, p_write = price(model)
    return (input_tokens * p_in + output_tokens * p_out + cache_read * p_read + cache_write * p_write) / 1_000_000


def worst_case_usd(input_tokens: int, max_output_tokens: int) -> float:
    """Upper bound for one call: every input token written to cache, every output token used, at the
    highest price any model in the fallback chain could charge."""
    p_in, p_out, _, p_write = UNKNOWN_PRICE
    return (input_tokens * max(p_in, p_write) + max_output_tokens * p_out) / 1_000_000


class Ledger:
    _lock = threading.Lock()

    def __init__(self, path: Path, cap: float | None = None):
        self.path = path
        env_cap = os.environ.get("CIVSPEAK_MONTHLY_CAP_USD")
        self.cap = cap if cap is not None else float(env_cap) if env_cap else DEFAULT_CAP_USD

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield f
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @staticmethod
    def _entries(f) -> list[dict]:
        f.seek(0)
        out = []
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    @staticmethod
    def _total(entries: list[dict], month: str) -> float:
        amounts: dict[str, float] = {}
        for e in entries:
            if e.get("month") != month:
                continue
            if e.get("state") == "reserved":
                amounts.setdefault(e["id"], float(e["usd"]))
            elif e.get("state") == "settled":
                amounts[e["id"]] = float(e["usd"])
        return sum(amounts.values())

    def month_total(self, month: str | None = None) -> float:
        with self._locked() as f:
            return self._total(self._entries(f), month or month_key())

    def reserve(self, usd: float) -> Reservation:
        month = month_key()
        with self._locked() as f:
            spent = self._total(self._entries(f), month)
            if spent + usd > self.cap:
                raise CapReached(spent, self.cap)
            rid = uuid.uuid4().hex
            f.write(json.dumps({"id": rid, "month": month, "state": "reserved", "usd": round(usd, 6), "t": time.time()}) + "\n")
            f.flush()
        return Reservation(self, rid, month)

    def _settle(self, rid: str, month: str, usd: float, detail: dict) -> float:
        with self._locked() as f:
            f.write(
                json.dumps({"id": rid, "month": month, "state": "settled", "usd": round(usd, 6), "t": time.time(), **detail})
                + "\n"
            )
            f.flush()
            return self._total(self._entries(f), month)


class Reservation:
    def __init__(self, ledger: Ledger, rid: str, month: str):
        self.ledger = ledger
        self.id = rid
        self.month = month
        self.settled = False

    def settle(self, usd: float, **detail) -> float:
        """Record the real cost; returns this month's new total."""
        self.settled = True
        return self.ledger._settle(self.id, self.month, usd, detail)
