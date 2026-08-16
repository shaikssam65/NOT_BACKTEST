from __future__ import annotations

import pandas as pd

from trading_bot.indicators import add_indicators, rule_score_row, snapshot_from_frame


def test_rule_score_buy_on_aligned_uptrend():
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
    assert score >= 68


def test_rule_score_avoid_on_downtrend_and_overbought():
    row = pd.Series(
        {
            "close": 90.0,
            "sma_fast": 92.0,
            "sma_slow": 100.0,
            "ema_fast": 91.0,
            "ema_slow": 97.0,
            "rsi": 78.0,
            "volume_ratio": 0.5,
        }
    )
    score, signal = rule_score_row(row)
    assert signal == "avoid"
    assert score <= 38


def test_add_indicators_appends_expected_columns(trending_frame=None):
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
    assert snap.sma_trend in {"bullish", "bearish", "unknown"}
