"""Simple bot tests."""

from __future__ import annotations

from datetime import date

from trading_bot.simple_bot import research_and_buy
from tests.conftest import FakeProvider, trending_ohlcv


def test_research_and_buy_runs(db, settings):
    frames = {
        "AAA": trending_ohlcv(n=100, start_price=120.0, seed=1),
        "BBB": trending_ohlcv(n=100, start_price=90.0, seed=2),
        "CCC": trending_ohlcv(n=100, start_price=150.0, seed=3),
        "DDD": trending_ohlcv(n=100, start_price=40.0, seed=4),
        "EEE": trending_ohlcv(n=100, start_price=80.0, seed=5),
    }
    # conftest ranks: AAA=1 filtered out (MIN_RANK=25), EEE=150 ok
    provider = FakeProvider(frames)
    result = research_and_buy(
        db,
        settings,
        provider,
        capital=60_000.0,
        pick_count=2,
        as_of=date(2024, 5, 15),
    )
    assert result["ok"] is True
    assert result["capital"] == 60_000.0
    assert "orders" in result
