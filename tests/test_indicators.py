from __future__ import annotations

import pandas as pd

from trading_bot.indicators import add_indicators, rule_score_row, snapshot_from_frame
from trading_bot.strategies import signal_sma_crossover, signal_trend_quality


def test_trend_quality_buy_requires_strict_alignment():
    row = pd.Series(
        {
            "close": 110.0,
            "sma_fast": 108.0,
            "sma_slow": 100.0,
            "ema_fast": 109.0,
            "ema_slow": 104.0,
            "rsi": 55.0,
            "volume_ratio": 1.3,
        }
    )
    score, signal = signal_trend_quality(row)
    assert signal == "buy"
    assert score >= 78


def test_trend_quality_rejects_overbought():
    row = pd.Series(
        {
            "close": 110.0,
            "sma_fast": 108.0,
            "sma_slow": 100.0,
            "ema_fast": 109.0,
            "ema_slow": 104.0,
            "rsi": 72.0,
            "volume_ratio": 1.3,
        }
    )
    _, signal = signal_trend_quality(row)
    assert signal != "buy"


def test_sma_crossover_avoid_on_downtrend():
    row = pd.Series(
        {
            "close": 90.0,
            "sma_fast": 92.0,
            "sma_slow": 100.0,
            "ema_fast": 91.0,
            "ema_slow": 97.0,
            "rsi": 45.0,
            "volume_ratio": 1.0,
        }
    )
    _, signal = signal_sma_crossover(row)
    assert signal == "avoid"


def test_rule_score_row_uses_trend_quality():
    row = pd.Series(
        {
            "close": 110.0,
            "sma_fast": 108.0,
            "sma_slow": 100.0,
            "ema_fast": 109.0,
            "ema_slow": 104.0,
            "rsi": 55.0,
            "volume_ratio": 1.3,
        }
    )
    score, signal = rule_score_row(row)
    assert signal == "buy"
    assert score >= 78


def test_add_indicators_appends_expected_columns():
    dates = pd.bdate_range("2024-01-02", periods=60)
    close = pd.Series(range(100, 160), dtype=float)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1_000_000,
        }
    )
    out = add_indicators(df)
    for col in ("sma_fast", "sma_slow", "ema_fast", "ema_slow", "rsi", "atr", "volume_ratio"):
        assert col in out.columns
    snap = snapshot_from_frame(out)
    assert snap.last_close == 159.0
