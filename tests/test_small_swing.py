"""Under-₹50 weekly + auto profit takes."""

from __future__ import annotations

from datetime import date

from trading_bot.execution import manage_open_positions
from trading_bot.small_swing import auto_take_profits, run_small_swing_trade
from trading_bot.strategies import PRIMARY_STRATEGIES, normalize_strategy
from tests.conftest import FakeProvider, trending_ohlcv


def test_combined_is_primary():
    assert "combined" in PRIMARY_STRATEGIES
    assert normalize_strategy("ensemble") == "combined"
    assert normalize_strategy("rules_and_agents") == "combined"


def test_auto_sell_at_30_pct(db, settings):
    db.execute(
        """
        INSERT INTO positions (
            opened_at, symbol, qty, entry_price, stop_loss, target,
            status, mode, strategy
        ) VALUES (
            '2026-08-01T04:00:00+00:00', 'CHEAP', 100, 50.0, 46.0, 65.0,
            'open', 'paper', 'small_swing'
        )
        """
    )
    db.commit()
    actions = manage_open_positions(
        db,
        settings,
        fallback_prices={"CHEAP": 70.0},  # +40%
    )
    assert any(a.get("action") == "sell_profit" for a in actions)
    open_n = db.execute("SELECT COUNT(*) AS n FROM positions WHERE status='open'").fetchone()["n"]
    assert open_n == 0


def test_paper_kite_profit_skipped(db, settings):
    report = auto_take_profits(db, settings, include_kite_holdings=False)
    assert "sold_bot" in report


def test_run_under50_splits(db, settings):
    # Prices under 50
    frames = {
        "AAA": trending_ohlcv(n=100, start_price=35.0, seed=1),
        "BBB": trending_ohlcv(n=100, start_price=40.0, seed=2),
        "CCC": trending_ohlcv(n=100, start_price=28.0, seed=3),
        "DDD": trending_ohlcv(n=100, start_price=45.0, seed=4),
        "EEE": trending_ohlcv(n=100, start_price=32.0, seed=5),
    }
    # Universe ranks in conftest: AAA=1..EEE=150 — bump prefer for test by using settings defaults
    # AAA rank 1 may be filtered (prefer_rank_above=30). EEE is 150 — ok.
    provider = FakeProvider(frames)
    result = run_small_swing_trade(
        db,
        settings,
        provider,
        as_of=date(2024, 5, 15),
        trade_capital=90_000.0,
        pick_count=3,
        take_profits_first=False,
    )
    assert result["strategy"] == "small_swing"
    assert result["trade_capital"] == 90_000.0
