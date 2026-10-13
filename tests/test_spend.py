"""The $10 monthly cap is enforced locally and can never be overshot."""

import pytest

from civspeak import spend


def test_reserve_settle_and_cap(tmp_path):
    ledger = spend.Ledger(tmp_path / "spend.jsonl", cap=1.0)
    r = ledger.reserve(0.6)
    assert ledger.month_total() == pytest.approx(0.6)
    assert r.settle(0.02, model="claude-opus-5-5") == pytest.approx(0.02)
    ledger.reserve(0.9).settle(0.5)
    assert ledger.month_total() == pytest.approx(0.52)
    with pytest.raises(spend.CapReached) as e:
        ledger.reserve(0.5)
    assert "Monthly limit reached" in str(e.value)


def test_unsettled_reservations_count_at_worst_case(tmp_path):
    ledger = spend.Ledger(tmp_path / "spend.jsonl", cap=1.0)
    ledger.reserve(0.7)  # e.g. the app crashed mid-answer
    assert spend.Ledger(tmp_path / "spend.jsonl", cap=1.0).month_total() == pytest.approx(0.7)
    with pytest.raises(spend.CapReached):
        ledger.reserve(0.4)


def test_months_are_separate(tmp_path, monkeypatch):
    ledger = spend.Ledger(tmp_path / "spend.jsonl", cap=1.0)
    monkeypatch.setattr(spend, "month_key", lambda t=None: "2026-09")
    ledger.reserve(0.9).settle(0.9)
    monkeypatch.setattr(spend, "month_key", lambda t=None: "2026-10")
    assert ledger.month_total() == 0
    ledger.reserve(0.9)


def test_prices_and_worst_case():
    # 1M input + 1M output on Opus 5.5 = $4 + $20.
    assert spend.cost_usd("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)
    assert spend.cost_usd("claude-opus-5-5", cache_read=1_000_000) == pytest.approx(0.20)
    # Unknown (fallback) models are priced high so the ledger never under-counts.
    assert spend.cost_usd("some-other-model", 1_000_000) > spend.cost_usd("claude-opus-5-5", 1_000_000)
    assert spend.worst_case_usd(5000, 2000) >= spend.cost_usd("claude-opus-5-5", 5000, 2000, 0, 0)


def test_cap_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CIVSPEAK_MONTHLY_CAP_USD", "2.5")
    assert spend.Ledger(tmp_path / "s.jsonl").cap == 2.5
    monkeypatch.delenv("CIVSPEAK_MONTHLY_CAP_USD")
    assert spend.Ledger(tmp_path / "s.jsonl").cap == 10.0
