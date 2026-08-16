from __future__ import annotations

import pandas as pd

from trading_bot.config import IndicatorConfig
from trading_bot.models import IndicatorSnapshot, Signal


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return _wilder(tr, period)


def add_indicators(df: pd.DataFrame, cfg: IndicatorConfig | None = None) -> pd.DataFrame:
    cfg = cfg or IndicatorConfig()
    out = df.copy()
    out["sma_fast"] = out["close"].rolling(cfg.sma_fast, min_periods=cfg.sma_fast).mean()
    out["sma_slow"] = out["close"].rolling(cfg.sma_slow, min_periods=cfg.sma_slow).mean()
    out["ema_fast"] = out["close"].ewm(span=cfg.ema_fast, adjust=False).mean()
    out["ema_slow"] = out["close"].ewm(span=cfg.ema_slow, adjust=False).mean()
    out["rsi"] = rsi(out["close"], cfg.rsi_period)
    out["atr"] = atr(out, cfg.atr_period)
    vol_sma = out["volume"].rolling(cfg.volume_sma, min_periods=cfg.volume_sma).mean()
    out["volume_ratio"] = out["volume"] / vol_sma.replace(0, pd.NA)
    return out


def rule_score_row(row: pd.Series) -> tuple[int, Signal]:
    """Default snapshot score uses the stricter trend_quality strategy."""
    from trading_bot.strategies import signal_trend_quality

    return signal_trend_quality(row)


def snapshot_from_frame(df: pd.DataFrame) -> IndicatorSnapshot:
    if df.empty:
        raise ValueError("cannot build indicator snapshot from empty frame")
    row = df.iloc[-1]
    score, signal = rule_score_row(row)
    sma_fast = _opt_float(row.get("sma_fast"))
    sma_slow = _opt_float(row.get("sma_slow"))
    ema_fast = _opt_float(row.get("ema_fast"))
    ema_slow = _opt_float(row.get("ema_slow"))
    if sma_fast is not None and sma_slow is not None:
        sma_trend = "bullish" if sma_fast > sma_slow else "bearish"
    else:
        sma_trend = "unknown"
    if ema_fast is not None and ema_slow is not None:
        ema_trend = "bullish" if ema_fast > ema_slow else "bearish"
    else:
        ema_trend = "unknown"
    return IndicatorSnapshot(
        last_close=float(row["close"]),
        sma_fast=sma_fast,
        sma_slow=sma_slow,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi=_opt_float(row.get("rsi")),
        atr=_opt_float(row.get("atr")),
        volume_ratio=_opt_float(row.get("volume_ratio")),
        sma_trend=sma_trend,
        ema_trend=ema_trend,
        rule_score=score,
        rule_signal=signal,
    )


def _opt_float(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)
