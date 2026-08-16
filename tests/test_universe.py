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


def test_seed_universe_is_total_market():
    stocks = load_seed_stocks()
    assert len(stocks) >= 700
    assert stocks[0].market_cap_rank == 1
    assert any("Nifty 50" in (s.indices or []) for s in stocks)
    assert any("Nifty Microcap 250" in (s.indices or []) for s in stocks)
    assert any("Nifty Smallcap250 Quality 50" in (s.indices or []) for s in stocks)
    assert any(
        "Nifty Smallcap250 Momentum Quality 100" in (s.indices or []) for s in stocks
    )
    assert any("Nifty Total Market" in (s.indices or []) for s in stocks)
    small50 = [s for s in stocks if "Nifty Smallcap 50" in (s.indices or [])]
    assert 40 <= len(small50) <= 60


def test_get_universe_seeds_empty_db(db):
    # conftest already persisted 5 names; confirm helper works on a fresh file via that fixture
    stocks = get_universe(db)
    assert len(stocks) == 5
    assert stocks[0].market_cap_rank == 1


def test_index_filter_on_seed():
    stocks = load_seed_stocks()
    nifty50 = [s for s in stocks if "Nifty 50" in (s.indices or [])]
    assert len(nifty50) == 50
    mid150 = [s for s in stocks if "Nifty Midcap 150" in (s.indices or [])]
    assert len(mid150) == 150
    micro = [s for s in stocks if "Nifty Microcap 250" in (s.indices or [])]
    assert 200 <= len(micro) <= 260
    mq = [s for s in stocks if "Nifty Smallcap250 Momentum Quality 100" in (s.indices or [])]
    assert len(mq) == 100
    q50 = [s for s in stocks if "Nifty Smallcap250 Quality 50" in (s.indices or [])]
    assert len(q50) == 50
    midsmall = [s for s in stocks if "Nifty MidSmallcap 400" in (s.indices or [])]
    assert len(midsmall) == 400
    total = [s for s in stocks if "Nifty Total Market" in (s.indices or [])]
    assert len(total) >= 700
