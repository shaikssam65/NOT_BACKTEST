from __future__ import annotations

from trading_bot.config import load_settings
from trading_bot.db import init_db
from trading_bot.risk import OrderIntent, daily_realized_pnl, validate_order


def test_validate_order_requires_stop(db, settings):
    decision = validate_order(
        OrderIntent("INFY", "BUY", 100.0, stop_loss_price=None),
        settings,
        db,
    )
    assert decision.passed is False
    assert decision.reason == "stop_loss_mandatory"


def test_validate_order_sizes_position(db, settings):
    decision = validate_order(
        OrderIntent("INFY", "BUY", 100.0, stop_loss_price=98.0, target_price=104.0),
        settings,
        db,
        available_capital=100_000,
    )
    assert decision.passed is True
    assert decision.qty == 500


def test_validate_order_blocks_duplicate_position(db, settings):
    db.execute(
        """
        INSERT INTO positions (
            opened_at, symbol, qty, entry_price, stop_loss, target, status, mode
        ) VALUES ('2026-08-16T10:00:00+00:00', 'INFY', 10, 100, 98, 104, 'open', 'paper')
        """
    )
    db.commit()
    decision = validate_order(
        OrderIntent("INFY", "BUY", 100.0, stop_loss_price=98.0),
        settings,
        db,
    )
    assert decision.passed is False
    assert "averaging" in decision.reason or "duplicate" in decision.reason


def test_manual_source_still_goes_through_gate(db, settings):
    decision = validate_order(
        OrderIntent("INFY", "BUY", 100.0, stop_loss_price=99.5, source="manual"),
        settings,
        db,
    )
    # 0.5% stop is at min bound — should pass sizing
    assert decision.passed is True
