"""Named trading strategies for backtest + daily auto-trade.

Quality-first: fewer signals, stricter confirmation. AI is always a filter on top
for strategies that end with _ai (and combined), never a standalone oracle.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from trading_bot.ai_layer import combine_signals
from trading_bot.models import Signal

StrategyName = Literal[
    "sma_crossover",
    "ema_crossover",
    "rsi_pullback",
    "trend_quality",
    "sma_ai",
    "ema_ai",
    "rsi_ai",
    "combined",
    "ensemble",
    # Legacy aliases kept for older dashboard/cache keys
    "rule_based",
    "ai_filtered",
]

VALID_STRATEGIES: tuple[StrategyName, ...] = (
    "ensemble",
    "sma_crossover",
    "ema_crossover",
    "rsi_pullback",
    "trend_quality",
    "sma_ai",
    "ema_ai",
    "rsi_ai",
    "combined",
)

STRATEGY_LABELS: dict[str, str] = {
    "ensemble": "Ensemble vote (4 rules + 2 AI agents) — recommended",
    "sma_crossover": "SMA 20/50 crossover (strict)",
    "ema_crossover": "EMA 12/26 crossover (strict)",
    "rsi_pullback": "Uptrend + RSI pullback buy",
    "trend_quality": "Multi-confirm trend (best rules-only)",
    "sma_ai": "SMA crossover + AI filter",
    "ema_ai": "EMA crossover + AI filter",
    "rsi_ai": "RSI pullback + AI filter",
    "combined": "Trend quality + AI",
    "rule_based": "Legacy → trend_quality",
    "ai_filtered": "Legacy → sma_ai",
}


def normalize_strategy(name: str) -> StrategyName:
    key = (name or "ensemble").strip().lower()
    aliases = {
        "rule_based": "trend_quality",
        "ai_filtered": "sma_ai",
        "voting": "ensemble",
        "multi_agent": "ensemble",
    }
    key = aliases.get(key, key)
    if key not in VALID_STRATEGIES and key not in aliases:
        # still allow legacy names in VALID for type checkers
        if key not in STRATEGY_LABELS:
            raise ValueError(f"Unknown strategy '{name}'. Choose from {list(STRATEGY_LABELS)}")
    return key  # type: ignore[return-value]


def _num(row: pd.Series, key: str) -> float | None:
    value = row.get(key)
    if value is None or pd.isna(value):
        return None
    return float(value)


def signal_sma_crossover(row: pd.Series) -> tuple[int, Signal]:
    """Buy only on bullish SMA stack + price above fast SMA + volume."""
    close = _num(row, "close")
    sma_fast = _num(row, "sma_fast")
    sma_slow = _num(row, "sma_slow")
    vol = _num(row, "volume_ratio")
    rsi_val = _num(row, "rsi")
    if None in (close, sma_fast, sma_slow, rsi_val):
        return 0, "hold"
    assert close is not None and sma_fast is not None and sma_slow is not None and rsi_val is not None
    if sma_fast <= sma_slow or close <= sma_fast:
        return (25, "avoid") if sma_fast < sma_slow else (45, "hold")
    if rsi_val > 68 or rsi_val < 40:
        return 40, "hold"
    if vol is not None and vol < 1.0:
        return 48, "hold"
    score = 72
    if vol is not None and vol >= 1.2:
        score += 8
    if 48 <= rsi_val <= 62:
        score += 8
    return min(100, score), "buy"


def signal_ema_crossover(row: pd.Series) -> tuple[int, Signal]:
    close = _num(row, "close")
    ema_fast = _num(row, "ema_fast")
    ema_slow = _num(row, "ema_slow")
    sma_slow = _num(row, "sma_slow")
    rsi_val = _num(row, "rsi")
    vol = _num(row, "volume_ratio")
    if None in (close, ema_fast, ema_slow, rsi_val):
        return 0, "hold"
    assert close is not None and ema_fast is not None and ema_slow is not None and rsi_val is not None
    if ema_fast <= ema_slow or close <= ema_fast:
        return (25, "avoid") if ema_fast < ema_slow else (45, "hold")
    # Prefer EMA buys that also sit above the slow SMA (higher timeframe bias).
    if sma_slow is not None and close < sma_slow:
        return 42, "hold"
    if rsi_val > 70 or rsi_val < 42:
        return 40, "hold"
    if vol is not None and vol < 0.95:
        return 46, "hold"
    score = 74
    if 50 <= rsi_val <= 62:
        score += 8
    if vol is not None and vol >= 1.15:
        score += 6
    return min(100, score), "buy"


def signal_rsi_pullback(row: pd.Series) -> tuple[int, Signal]:
    """Buy dips in an established uptrend (SMA bullish, RSI cooled to 40-55)."""
    close = _num(row, "close")
    sma_fast = _num(row, "sma_fast")
    sma_slow = _num(row, "sma_slow")
    rsi_val = _num(row, "rsi")
    vol = _num(row, "volume_ratio")
    if None in (close, sma_fast, sma_slow, rsi_val):
        return 0, "hold"
    assert close is not None and sma_fast is not None and sma_slow is not None and rsi_val is not None
    if sma_fast <= sma_slow:
        return 30, "avoid"
    if close < sma_slow:
        return 35, "hold"
    if not (40 <= rsi_val <= 55):
        if rsi_val > 70:
            return 28, "avoid"
        return 45, "hold"
    if vol is not None and vol < 0.9:
        return 48, "hold"
    score = 76 + (8 if close >= sma_fast else 0)
    return min(100, score), "buy"


def signal_trend_quality(row: pd.Series) -> tuple[int, Signal]:
    """Stricter multi-confirm trend — fewer trades, higher bar for 'buy'."""
    close = _num(row, "close")
    sma_fast = _num(row, "sma_fast")
    sma_slow = _num(row, "sma_slow")
    ema_fast = _num(row, "ema_fast")
    ema_slow = _num(row, "ema_slow")
    rsi_val = _num(row, "rsi")
    vol = _num(row, "volume_ratio")
    if None in (close, sma_fast, sma_slow, ema_fast, ema_slow, rsi_val):
        return 0, "hold"
    assert None not in (close, sma_fast, sma_slow, ema_fast, ema_slow, rsi_val)
    score = 50
    # Hard vetoes first
    if sma_fast < sma_slow or ema_fast < ema_slow:
        return 22, "avoid"
    if close < sma_fast:
        return 40, "hold"
    if rsi_val > 65 or rsi_val < 45:
        return 42, "hold"
    if vol is not None and vol < 1.05:
        return 46, "hold"

    score += 20  # SMA aligned
    score += 15  # EMA aligned
    score += 10  # price > SMA fast
    if 48 <= rsi_val <= 60:
        score += 12
    if vol is not None and vol >= 1.2:
        score += 8
    score = int(max(0, min(100, score)))
    # Higher threshold than the old 68 bar — quality over quantity
    if score >= 78:
        return score, "buy"
    if score <= 35:
        return score, "avoid"
    return score, "hold"


_RULE_FNS = {
    "sma_crossover": signal_sma_crossover,
    "ema_crossover": signal_ema_crossover,
    "rsi_pullback": signal_rsi_pullback,
    "trend_quality": signal_trend_quality,
    "rule_based": signal_trend_quality,
}


def rule_signal_for(strategy: str, row: pd.Series) -> tuple[int, Signal]:
    name = normalize_strategy(strategy)
    base = {
        "sma_crossover": "sma_crossover",
        "sma_ai": "sma_crossover",
        "ema_crossover": "ema_crossover",
        "ema_ai": "ema_crossover",
        "rsi_pullback": "rsi_pullback",
        "rsi_ai": "rsi_pullback",
        "trend_quality": "trend_quality",
        "combined": "trend_quality",
        "ensemble": "trend_quality",
        "rule_based": "trend_quality",
        "ai_filtered": "sma_crossover",
    }.get(name, "trend_quality")
    return _RULE_FNS[base](row)


def needs_ai(strategy: str) -> bool:
    name = normalize_strategy(strategy)
    return name in {"sma_ai", "ema_ai", "rsi_ai", "combined", "ai_filtered", "ensemble"}


def final_signal(strategy: str, rule: Signal, ai: Signal) -> Signal:
    name = normalize_strategy(strategy)
    if name in {"sma_crossover", "ema_crossover", "rsi_pullback", "trend_quality", "rule_based"}:
        return rule
    if name in {"sma_ai", "ema_ai", "rsi_ai", "ai_filtered"}:
        return ai if rule == "buy" else "hold"
    if name == "ensemble":
        # Handled by trading_bot.ensemble.vote_symbol — keep compatible fallback
        return combine_signals(rule, ai)
    # combined
    return combine_signals(rule, ai)
