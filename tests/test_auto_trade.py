"""Auto-trade smoke test with fake data provider."""

from __future__ import annotations

from datetime import date

from trading_bot.auto_trade import run_daily_auto_trade
from tests.conftest import FakeProvider, trending_ohlcv


def test_auto_trade_ensemble_returns_activity(db, settings):
    frames = {
        "AAA": trending_ohlcv(n=100, start_price=200.0, seed=1),
        "BBB": trending_ohlcv(n=100, start_price=150.0, seed=2),
        "CCC": trending_ohlcv(n=100, start_price=120.0, seed=3),
        "DDD": trending_ohlcv(n=100, start_price=20.0, seed=4),
        "EEE": trending_ohlcv(n=100, start_price=80.0, seed=5),
    }
    provider = FakeProvider(frames)
    result = run_daily_auto_trade(
        db,
        settings,
        provider,
        strategy="ensemble",
        as_of=date(2024, 5, 15),
        use_llm=False,
        manage_exits=True,
        trade_capital=100_000.0,
    )
    assert result["mode"] == "paper"
    assert result["strategy"] == "ensemble"
    assert result["trade_capital"] == 100_000.0
    assert result["scanned"] >= 1
    assert isinstance(result["activity"], list) and len(result["activity"]) >= 4
    assert "orders" in result
    assert "detected" in result
    assert "entry_plans" in result
    assert "open_positions" in result
