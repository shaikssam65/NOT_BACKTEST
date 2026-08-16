from __future__ import annotations

from datetime import date

from trading_bot.backtest import backtest, position_qty
from tests.conftest import FakeProvider, falling_ohlcv, trending_ohlcv


def test_position_qty_formula():
    qty = position_qty(capital=100_000, risk_pct=1.0, entry=100.0, stop=98.0)
    assert qty == 500


def test_position_qty_zero_when_stop_not_below_entry():
    assert position_qty(100_000, 1.0, 100.0, 100.0) == 0
    assert position_qty(100_000, 1.0, 100.0, 101.0) == 0


def test_backtest_next_bar_entry_and_mandatory_stop(db, settings):
    frame = trending_ohlcv(n=160, seed=11)
    provider = FakeProvider({"RELIANCE": frame})
    start = date(2024, 3, 1)
    end = date(2024, 7, 31)
    result = backtest(
        "RELIANCE",
        "rule_based",
        start,
        end,
        100_000,
        settings,
        provider,
        db,
        use_llm=False,
    )
    assert result.symbol == "RELIANCE"
    assert result.max_drawdown_pct >= 0
    assert len(result.equity_curve) > 10
    if result.number_of_trades:
        first = result.trades[0]
        assert first["stop_loss"] < first["entry_price"]
        assert first["target"] > first["entry_price"]


def test_backtest_cache_hit(db, settings):
    frame = trending_ohlcv(n=140, seed=5)
    provider = FakeProvider({"INFY": frame})
    kwargs = dict(
        symbol="INFY",
        strategy_name="combined",
        start_date="2024-03-01",
        end_date="2024-06-28",
        capital=50_000,
        settings=settings,
        provider=provider,
        conn=db,
        use_llm=False,
    )
    first = backtest(**kwargs)
    second = backtest(**kwargs)
    assert first.cached is False
    assert second.cached is True
    assert second.total_return_pct == first.total_return_pct


def test_backtest_falling_market_does_not_crash(db, settings):
    provider = FakeProvider({"SBIN": falling_ohlcv(n=140, seed=2)})
    result = backtest(
        "SBIN",
        "combined",
        "2024-03-01",
        "2024-06-28",
        100_000,
        settings,
        provider,
        db,
        use_llm=False,
    )
    assert result.number_of_trades >= 0
    assert result.ending_equity > 0
