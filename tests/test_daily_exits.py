"""Daily-style exits: stop/target only — never force-flat by calendar day."""

from __future__ import annotations

from datetime import date, timedelta

from trading_bot.execution import manage_open_positions


def test_does_not_force_sell_yesterday_winner(db, settings):
    yesterday = (date.today() - timedelta(days=1)).isoformat() + "T04:00:00+00:00"
    db.execute(
        """
        INSERT INTO positions (
            opened_at, symbol, qty, entry_price, stop_loss, target, status, mode, strategy
        ) VALUES (?, 'INFY', 10, 100.0, 97.0, 105.0, 'open', 'paper', 'ensemble')
        """,
        (yesterday,),
    )
    db.commit()
    actions = manage_open_positions(
        db,
        settings,
        as_of=date.today(),
        fallback_prices={"INFY": 101.0},  # between stop and target
    )
    assert actions and actions[0]["action"] == "hold"
    open_n = db.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE status='open'"
    ).fetchone()["n"]
    assert open_n == 1


def test_sells_on_stop_not_on_time(db, settings):
    yesterday = (date.today() - timedelta(days=1)).isoformat() + "T04:00:00+00:00"
    db.execute(
        """
        INSERT INTO positions (
            opened_at, symbol, qty, entry_price, stop_loss, target, status, mode, strategy
        ) VALUES (?, 'TCS', 5, 100.0, 98.0, 104.0, 'open', 'paper', 'ensemble')
        """,
        (yesterday,),
    )
    db.commit()
    actions = manage_open_positions(
        db,
        settings,
        fallback_prices={"TCS": 97.5},
    )
    assert any(a.get("action") == "sell_stop" for a in actions)
