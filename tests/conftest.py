from __future__ import annotations

from datetime import date

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from trading_bot.config import load_settings
from trading_bot.db import init_db
from trading_bot.models import AISignal, Candidate, IndicatorSnapshot, UniverseStock
from trading_bot.universe import persist_universe


class FakeProvider:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def get_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        df = self.frames[symbol.upper()].copy()
        mask = (df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))
        return df.loc[mask].reset_index(drop=True)


def trending_ohlcv(
    n: int = 120,
    start_price: float = 120.0,
    drift: float = 0.003,
    vol: float = 0.008,
    seed: int = 7,
    start: str = "2024-01-02",
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n)
    rets = rng.normal(drift, vol, n)
    close = start_price * np.cumprod(1 + rets)
    close = np.maximum(close, 5.0)
    high = close * (1 + rng.uniform(0.002, 0.012, n))
    low = close * (1 - rng.uniform(0.002, 0.012, n))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    open_ = np.clip(open_, low, high)
    volume = rng.integers(800_000, 2_000_000, n)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": open_,
            "high": np.maximum(high, np.maximum(open_, close)),
            "low": np.minimum(low, np.minimum(open_, close)),
            "close": close,
            "volume": volume,
        }
    )


def falling_ohlcv(n: int = 120, start_price: float = 120.0, seed: int = 3) -> pd.DataFrame:
    return trending_ohlcv(n=n, start_price=start_price, drift=-0.004, vol=0.01, seed=seed)


def make_snapshot(
    last_close: float = 100.0,
    rule_signal: str = "buy",
    rule_score: int = 80,
    rsi: float = 55.0,
) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        last_close=last_close,
        sma_fast=102.0,
        sma_slow=98.0,
        ema_fast=101.0,
        ema_slow=99.0,
        rsi=rsi,
        atr=2.0,
        volume_ratio=1.2,
        sma_trend="bullish",
        ema_trend="bullish",
        rule_score=rule_score,
        rule_signal=rule_signal,  # type: ignore[arg-type]
    )


def make_candidate(
    symbol: str,
    rank: int,
    price: float,
    score: float,
    source: str = "ai_selected",
) -> Candidate:
    stock = UniverseStock(symbol=symbol, name=symbol, market_cap_rank=rank, last_price=price)
    ai = AISignal(
        signal="buy",
        confidence=80,
        stop_loss_pct=2.0,
        target_pct=4.0,
        reasoning="test",
        source="heuristic_fallback",
    )
    return Candidate(
        stock=stock,
        indicators=make_snapshot(last_close=price),
        ai=ai,
        combined_signal="buy",
        combined_score=score,
        source=source,  # type: ignore[arg-type]
    )


@pytest.fixture
def settings():
    # Tests never hit the live Kite path.
    return replace(load_settings(), paper_mode=True)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    persist_universe(
        conn,
        [
            UniverseStock("AAA", "AAA Ltd", 1, 200.0, "AAA.NS"),
            UniverseStock("BBB", "BBB Ltd", 2, 150.0, "BBB.NS"),
            UniverseStock("CCC", "CCC Ltd", 3, 120.0, "CCC.NS"),
            UniverseStock("DDD", "DDD Ltd", 4, 20.0, "DDD.NS"),
            UniverseStock("EEE", "EEE Ltd", 150, 80.0, "EEE.NS"),
        ],
    )
    return conn
