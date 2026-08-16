from __future__ import annotations

from trading_bot.config import load_settings
from trading_bot.universe import get_universe, load_seed_stocks


def test_paper_mode_defaults_true(monkeypatch):
    monkeypatch.delenv("PAPER_MODE", raising=False)
    settings = load_settings()
    assert settings.paper_mode is True


def test_paper_mode_false_only_when_explicit(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "false")
    settings = load_settings()
    assert settings.paper_mode is False


def test_seed_universe_has_200_and_top100_split():
    stocks = load_seed_stocks()
    assert len(stocks) == 200
    assert sum(1 for s in stocks if s.in_top100) == 100
    assert stocks[0].symbol == "RELIANCE"
    assert any(s.symbol == "IDEA" and s.last_price is not None and s.last_price < 50 for s in stocks)


def test_get_universe_seeds_empty_db(db):
    # conftest already persisted 5 names; confirm helper works on a fresh file via that fixture
    stocks = get_universe(db)
    assert len(stocks) == 5
    assert stocks[0].market_cap_rank == 1
