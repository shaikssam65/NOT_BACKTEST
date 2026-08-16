from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Signal = Literal["buy", "hold", "avoid"]
SelectionSource = Literal["ai_selected", "manual"]
StrategyName = Literal[
    "rules_combo",
    "dual_agents",
    "small_swing",
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
]


@dataclass
class UniverseStock:
    symbol: str
    name: str
    market_cap_rank: int
    last_price: float | None = None
    yahoo_ticker: str | None = None

    @property
    def in_top100(self) -> bool:
        return self.market_cap_rank <= 100


@dataclass
class IndicatorSnapshot:
    last_close: float
    sma_fast: float | None
    sma_slow: float | None
    ema_fast: float | None
    ema_slow: float | None
    rsi: float | None
    atr: float | None
    volume_ratio: float | None
    sma_trend: str
    ema_trend: str
    rule_score: int
    rule_signal: Signal

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AISignal:
    signal: Signal
    confidence: int
    stop_loss_pct: float
    target_pct: float
    reasoning: str
    source: str
    raw_response: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


@dataclass
class Candidate:
    stock: UniverseStock
    indicators: IndicatorSnapshot
    ai: AISignal
    combined_signal: Signal
    combined_score: float
    source: SelectionSource = "ai_selected"

    @property
    def symbol(self) -> str:
        return self.stock.symbol

    @property
    def last_price(self) -> float:
        return self.indicators.last_close

    def to_record(self, selection_date: str) -> dict[str, Any]:
        return {
            "selection_date": selection_date,
            "symbol": self.stock.symbol,
            "source": self.source,
            "rule_signal": self.indicators.rule_signal,
            "ai_signal": self.ai.signal,
            "combined_signal": self.combined_signal,
            "confidence": self.ai.confidence,
            "entry_price_target": self.indicators.last_close,
            "stop_loss_pct": self.ai.stop_loss_pct,
            "target_pct": self.ai.target_pct,
            "reasoning": self.ai.reasoning,
        }


@dataclass
class Trade:
    symbol: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    return_pct: float
    reason: str
    stop_loss: float
    target: float


@dataclass
class BacktestResult:
    symbol: str
    strategy_name: str
    start_date: str
    end_date: str
    capital: float
    ending_equity: float
    total_return_pct: float
    win_rate: float
    max_drawdown_pct: float
    number_of_trades: int
    wins: int
    losses: int
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    commentary: str = ""
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
