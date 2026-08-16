from __future__ import annotations

from trading_bot.export_backtest import summary_frame, to_csv_bytes, trades_frame


def test_export_csv_bytes():
    payload = {
        "symbol": "RELIANCE",
        "strategy_name": "combined",
        "start_date": "2024-01-01",
        "end_date": "2024-06-01",
        "capital": 100000,
        "ending_equity": 105000,
        "total_return_pct": 5.0,
        "win_rate": 60.0,
        "max_drawdown_pct": 2.0,
        "number_of_trades": 2,
        "wins": 1,
        "losses": 1,
        "cached": False,
        "commentary": "ok",
        "trades": [
            {
                "symbol": "RELIANCE",
                "entry_date": "2024-02-01",
                "exit_date": "2024-02-10",
                "entry_price": 100,
                "exit_price": 105,
                "qty": 10,
                "pnl": 50,
                "return_pct": 5,
                "reason": "target",
                "stop_loss": 98,
                "target": 105,
            }
        ],
        "equity_curve": [{"date": "2024-02-01", "equity": 100000}],
    }
    summary = to_csv_bytes(summary_frame(payload))
    trades = to_csv_bytes(trades_frame(payload))
    assert b"RELIANCE" in summary
    assert b"total_return_pct" in summary
    assert b"entry_price" in trades
    assert b"105" in trades
