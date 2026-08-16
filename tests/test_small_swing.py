"""Small-swing strategy + human-approved profit sells."""

from __future__ import annotations

from datetime import date

from trading_bot.small_swing import execute_approved_sells, run_small_swing_trade
from trading_bot.strategies import PRIMARY_STRATEGIES, normalize_strategy
from tests.conftest import FakeProvider, trending_ohlcv


def test_small_swing_is_primary():
    assert "small_swing" in PRIMARY_STRATEGIES
    assert normalize_strategy("swing_30") == "small_swing"


def test_propose_profit_exits_bot_position(db, settings):
    # Open position already up >30% — approval path tested via execute_approved_sells
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
    proposal = {
        "source": "bot_position",
        "symbol": "CHEAP",
        "qty": 100,
        "entry_price": 50.0,
        "ltp": 70.0,
        "pnl_pct": 40.0,
        "position_id": db.execute("SELECT id FROM positions WHERE symbol='CHEAP'").fetchone()["id"],
    }
    assert execute_approved_sells(db, settings, []) == []

    results = execute_approved_sells(db, settings, [proposal])
    assert results and results[0]["ok"] is True
    open_n = db.execute("SELECT COUNT(*) AS n FROM positions WHERE status='open'").fetchone()["n"]
    assert open_n == 0


def test_paper_skips_real_kite_holding_sell(db, settings):
    results = execute_approved_sells(
        db,
        settings,
        [
            {
                "source": "kite_holding",
                "symbol": "INFY",
                "qty": 10,
                "ltp": 1500.0,
            }
        ],
    )
    assert results[0]["ok"] is True
    assert results[0]["mode"] == "paper"
    assert "paper_skip" in results[0]["reason"]


def test_run_small_swing_splits_capital(db, settings):
    frames = {
        "AAA": trending_ohlcv(n=100, start_price=80.0, seed=1),
        "BBB": trending_ohlcv(n=100, start_price=60.0, seed=2),
        "CCC": trending_ohlcv(n=100, start_price=90.0, seed=3),
        "DDD": trending_ohlcv(n=100, start_price=40.0, seed=4),
        "EEE": trending_ohlcv(n=100, start_price=70.0, seed=5),
    }
    provider = FakeProvider(frames)
    result = run_small_swing_trade(
        db,
        settings,
        provider,
        as_of=date(2024, 5, 15),
        trade_capital=90_000.0,
        pick_count=3,
    )
    assert result["strategy"] == "small_swing"
    assert result["trade_capital"] == 90_000.0
    assert "activity" in result
    # If picks exist, slice capital should be capital/n
    if result["picks"]:
        assert abs(result["slice_capital"] - (90_000.0 / len(result["picks"]))) < 1.0
        for plan in result.get("entry_plans") or []:
            assert plan["target"] > plan["entry"] * 1.25  # ~30% target
