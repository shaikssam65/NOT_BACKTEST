from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from trading_bot.config import ROOT


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def summary_frame(payload: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": payload.get("symbol"),
                "strategy_name": payload.get("strategy_name"),
                "start_date": payload.get("start_date"),
                "end_date": payload.get("end_date"),
                "capital": payload.get("capital"),
                "ending_equity": payload.get("ending_equity"),
                "total_return_pct": payload.get("total_return_pct"),
                "win_rate": payload.get("win_rate"),
                "max_drawdown_pct": payload.get("max_drawdown_pct"),
                "number_of_trades": payload.get("number_of_trades"),
                "wins": payload.get("wins"),
                "losses": payload.get("losses"),
                "cached": payload.get("cached"),
                "commentary": payload.get("commentary"),
                "exported_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
        ]
    )


def trades_frame(payload: dict[str, Any]) -> pd.DataFrame:
    trades = payload.get("trades") or []
    if not trades:
        return pd.DataFrame(
            columns=[
                "symbol",
                "entry_date",
                "exit_date",
                "entry_price",
                "exit_price",
                "qty",
                "pnl",
                "return_pct",
                "reason",
                "stop_loss",
                "target",
            ]
        )
    return pd.DataFrame(trades)


def equity_frame(payload: dict[str, Any]) -> pd.DataFrame:
    curve = payload.get("equity_curve") or []
    if not curve:
        return pd.DataFrame(columns=["date", "equity"])
    return pd.DataFrame(curve)


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8")


def export_dir() -> Path:
    path = ROOT / "data" / "exports" / "backtests"
    path.mkdir(parents=True, exist_ok=True)
    return path


def try_save_csv_files(payload: dict[str, Any]) -> dict[str, str]:
    """Best-effort disk save (works locally; may be ephemeral on Streamlit Cloud)."""
    symbol = str(payload.get("symbol") or "UNKNOWN")
    strategy = str(payload.get("strategy_name") or "strategy")
    base = f"{symbol}_{strategy}_{_stamp()}"
    saved: dict[str, str] = {}
    try:
        out = export_dir()
        summary_path = out / f"{base}_summary.csv"
        trades_path = out / f"{base}_trades.csv"
        equity_path = out / f"{base}_equity.csv"
        summary_frame(payload).to_csv(summary_path, index=False)
        trades_frame(payload).to_csv(trades_path, index=False)
        equity_frame(payload).to_csv(equity_path, index=False)
        saved = {
            "summary": str(summary_path),
            "trades": str(trades_path),
            "equity": str(equity_path),
        }
    except OSError:
        pass
    return saved
