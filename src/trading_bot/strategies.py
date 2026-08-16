"""Named trading strategies for backtest + daily auto-trade.

Only two primary modes (what the UI shows):
  1) rules_combo  — 6 rule-based voters combined
  2) dual_agents  — Agent-Trend + Agent-Risk
Legacy names still normalize for old caches/CLI.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from trading_bot.models import Signal

StrategyName = Literal[
    "rules_combo",
    "dual_agents",
    # Legacy (normalize → primary)
    "ensemble",
    "sma_crossover",
    "ema_crossover",
    "rsi_pullback",
    "trend_quality",
    "sma_ai",
    "ema_ai",
    "rsi_ai",
    "combined",
    "rule_based",
    "ai_filtered",
    "momentum",
    "volume_thrust",
]

# Only these appear in the dashboard / recommended CLI choices.
PRIMARY_STRATEGIES: tuple[StrategyName, ...] = ("rules_combo", "dual_agents")

VALID_STRATEGIES: tuple[StrategyName, ...] = PRIMARY_STRATEGIES

STRATEGY_LABELS: dict[str, str] = {
    "rules_combo": "1 · Rules combo (6 rule voters)",
    "dual_agents": "2 · Dual agents (Agent-Trend + Agent-Risk)",
    # Legacy labels (still resolvable, not shown in UI)
    "ensemble": "Legacy → rules_combo",
    "sma_crossover": "Legacy rule",
    "ema_crossover": "Legacy rule",
    "rsi_pullback": "Legacy rule",
    "trend_quality": "Legacy rule",
    "sma_ai": "Legacy → dual_agents",
    "ema_ai": "Legacy → dual_agents",
    "rsi_ai": "Legacy → dual_agents",
    "combined": "Legacy → dual_agents",
    "rule_based": "Legacy → rules_combo",
    "ai_filtered": "Legacy → dual_agents",
}


def normalize_strategy(name: str) -> StrategyName:
    key = (name or "rules_combo").strip().lower()
    aliases = {
        "rule_based": "rules_combo",
        "voting": "rules_combo",
        "rules": "rules_combo",
        "rules_only": "rules_combo",
        "ensemble": "rules_combo",
        "sma_crossover": "rules_combo",
        "ema_crossover": "rules_combo",
        "rsi_pullback": "rules_combo",
        "trend_quality": "rules_combo",
        "momentum": "rules_combo",
        "volume_thrust": "rules_combo",
        "ai_filtered": "dual_agents",
        "multi_agent": "dual_agents",
        "agents": "dual_agents",
        "ai_agents": "dual_agents",
        "sma_ai": "dual_agents",
        "ema_ai": "dual_agents",
        "rsi_ai": "dual_agents",
        "combined": "dual_agents",
    }
    key = aliases.get(key, key)
    if key not in PRIMARY_STRATEGIES:
        raise ValueError(f"Unknown strategy '{name}'. Choose from {list(PRIMARY_STRATEGIES)}")
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


def signal_momentum(row: pd.Series) -> tuple[int, Signal]:
    """Short-horizon momentum: price above both SMAs, RSI constructive, ATR not exploding."""
    close = _num(row, "close")
    sma_fast = _num(row, "sma_fast")
    sma_slow = _num(row, "sma_slow")
    rsi_val = _num(row, "rsi")
    atr_val = _num(row, "atr")
    if None in (close, sma_fast, sma_slow, rsi_val):
        return 0, "hold"
    assert close is not None and sma_fast is not None and sma_slow is not None and rsi_val is not None
    if close < sma_slow or sma_fast < sma_slow:
        return 28, "avoid"
    if close < sma_fast:
        return 44, "hold"
    if rsi_val < 48 or rsi_val > 68:
        return 42, "hold"
    # Skip if ATR is huge vs price (too wild for daily style)
    if atr_val is not None and close > 0 and (atr_val / close) * 100 > 4.5:
        return 40, "hold"
    score = 74
    if 52 <= rsi_val <= 62:
        score += 10
    if close >= sma_fast * 1.005:
        score += 6
    return min(100, score), "buy"


def signal_volume_thrust(row: pd.Series) -> tuple[int, Signal]:
    """Volume confirmation with bullish structure."""
    close = _num(row, "close")
    sma_fast = _num(row, "sma_fast")
    sma_slow = _num(row, "sma_slow")
    ema_fast = _num(row, "ema_fast")
    ema_slow = _num(row, "ema_slow")
    vol = _num(row, "volume_ratio")
    rsi_val = _num(row, "rsi")
    if None in (close, sma_fast, sma_slow, ema_fast, ema_slow, rsi_val):
        return 0, "hold"
    assert None not in (close, sma_fast, sma_slow, ema_fast, ema_slow, rsi_val)
    if sma_fast < sma_slow or ema_fast < ema_slow:
        return 25, "avoid"
    if vol is None or vol < 1.15:
        return 45, "hold"
    if rsi_val > 70:
        return 30, "avoid"
    if close < sma_fast:
        return 42, "hold"
    score = 70 + (10 if vol >= 1.35 else 0) + (8 if 48 <= rsi_val <= 65 else 0)
    return min(100, score), "buy"


_RULE_FNS = {
    "sma_crossover": signal_sma_crossover,
    "ema_crossover": signal_ema_crossover,
    "rsi_pullback": signal_rsi_pullback,
    "trend_quality": signal_trend_quality,
    "momentum": signal_momentum,
    "volume_thrust": signal_volume_thrust,
    "rule_based": signal_trend_quality,
}

# Six rule voters for rules_combo
RULE_COMBO_VOTERS: tuple[tuple[str, object], ...] = (
    ("sma_crossover", signal_sma_crossover),
    ("ema_crossover", signal_ema_crossover),
    ("rsi_pullback", signal_rsi_pullback),
    ("trend_quality", signal_trend_quality),
    ("momentum", signal_momentum),
    ("volume_thrust", signal_volume_thrust),
)


def rules_combo_vote(row: pd.Series, *, min_buys: int = 3) -> tuple[int, Signal, dict[str, str]]:
    """Combine 6 rule strategies by vote. Returns avg_score, signal, vote map."""
    votes: dict[str, str] = {}
    scores: list[int] = []
    for name, fn in RULE_COMBO_VOTERS:
        score, signal = fn(row)  # type: ignore[operator]
        votes[name] = signal
        scores.append(int(score))
    buy_n = sum(1 for s in votes.values() if s == "buy")
    avoid_n = sum(1 for s in votes.values() if s == "avoid")
    avg = int(round(sum(scores) / max(len(scores), 1)))
    if avoid_n >= 4:
        return avg, "avoid", votes
    if buy_n >= min_buys:
        return min(100, avg + buy_n * 2), "buy", votes
    if buy_n == 0 and avoid_n >= 2:
        return avg, "avoid", votes
    return avg, "hold", votes



def rule_signal_for(strategy: str, row: pd.Series) -> tuple[int, Signal]:
    name = normalize_strategy(strategy)
    if name == "rules_combo":
        score, signal, _votes = rules_combo_vote(row, min_buys=3)
        return score, signal
    # dual_agents: rules are informational only; soft trend_quality for snapshot
    return signal_trend_quality(row)


def needs_ai(strategy: str) -> bool:
    return normalize_strategy(strategy) == "dual_agents"


def final_signal(strategy: str, rule: Signal, ai: Signal) -> Signal:
    name = normalize_strategy(strategy)
    if name == "rules_combo":
        return rule
    # dual_agents — AI agents decide (ai arg already reflects agent agreement in auto-trade)
    return ai

