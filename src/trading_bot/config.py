from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "settings.yaml"


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SelectionConfig:
    pick_count_min: int = 3
    pick_count_max: int = 4
    min_from_top100: int = 3
    max_below_price: int = 1
    below_price_threshold: float = 50.0
    lookback_days: int = 60
    candidate_pool: int = 20


@dataclass(frozen=True)
class AIConfig:
    model: str = "gpt-4o"
    temperature: float = 0.2
    max_tokens: int = 400
    timeout_seconds: int = 30


@dataclass(frozen=True)
class IndicatorConfig:
    sma_fast: int = 20
    sma_slow: int = 50
    ema_fast: int = 12
    ema_slow: int = 26
    rsi_period: int = 14
    atr_period: int = 14
    volume_sma: int = 20


@dataclass(frozen=True)
class BacktestConfig:
    slippage_pct: float = 0.05
    commission_pct: float = 0.03
    default_stop_loss_pct: float = 2.0
    default_target_pct: float = 4.0
    atr_stop_mult: float = 2.0
    enter_on_next_open: bool = True


@dataclass(frozen=True)
class RiskConfig:
    """Parameters only in Phase 1. The hard order gate lands in Phase 3."""

    risk_per_trade_pct: float = 1.0
    daily_loss_limit_pct: float = 3.0
    max_concurrent_positions: int = 4
    allow_averaging_down: bool = False
    min_stop_loss_pct: float = 0.5
    max_stop_loss_pct: float = 8.0


@dataclass(frozen=True)
class AutoTradeConfig:
    """Daily-trading style: short-horizon setups — exits via stop/target, not forced flatten."""

    default_strategy: str = "rules_combo"
    # Soft horizon for messaging / AI prompts only. Does NOT force-sell losers.
    intended_hold_days: int = 1
    # Day-trade style defaults when AI does not override.
    daily_stop_loss_pct: float = 1.5
    daily_target_pct: float = 3.0


@dataclass(frozen=True)
class SmallSwingConfig:
    """Under-₹50 established names: weekly buys, auto-sell at +30% (no human approval)."""

    pick_count: int = 3
    max_price: float = 50.0
    # Medium established: avoid mega-caps and micro junk.
    prefer_rank_above: int = 30
    prefer_rank_below: int = 180
    target_pct: float = 30.0
    stop_loss_pct: float = 8.0
    min_profit_sell_pct: float = 30.0
    equal_split: bool = True
    # Schedule hints (scripts enforce these)
    daily_exit_ist_hour: int = 10
    weekly_buy_weekday: int = 0  # Monday


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _session_access_token() -> str | None:
    path = ROOT / "data" / "kite_session.json"
    if not path.exists():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _nonempty(str(data.get("access_token") or ""))


@dataclass(frozen=True)
class Settings:
    paper_mode: bool
    capital: float
    database_path: Path
    openai_api_key: str | None
    finnhub_api_key: str | None
    kite_api_key: str | None
    kite_api_secret: str | None
    kite_access_token: str | None
    kite_redirect_url: str
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    indicators: IndicatorConfig = field(default_factory=IndicatorConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    auto_trade: AutoTradeConfig = field(default_factory=AutoTradeConfig)
    small_swing: SmallSwingConfig = field(default_factory=SmallSwingConfig)

    @property
    def kite_ready(self) -> bool:
        return bool(self.kite_api_key and self.kite_access_token)

    @property
    def openai_ready(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def finnhub_ready(self) -> bool:
        return bool(self.finnhub_api_key)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name) or {}
    if not isinstance(value, dict):
        raise ValueError(f"config section '{name}' must be a mapping")
    return value


def load_settings(config_path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    path = config_path or CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("settings.yaml must be a mapping")
        raw = loaded

    # Live mode is env-gated. YAML cannot silently turn paper trading off.
    paper_mode = _as_bool(os.getenv("PAPER_MODE"), default=False)

    db_env = os.getenv("DATABASE_PATH")
    database_path = Path(db_env) if db_env else ROOT / "data" / "db" / "trading.db"
    if not database_path.is_absolute():
        database_path = ROOT / database_path

    model = os.getenv("OPENAI_MODEL") or _section(raw, "ai").get("model", "gpt-4o")

    redirect = _nonempty(os.getenv("KITE_REDIRECT_URL")) or "http://127.0.0.1:8501/"

    return Settings(
        paper_mode=paper_mode,
        capital=float(raw.get("capital", 100000.0)),
        database_path=database_path,
        openai_api_key=_nonempty(os.getenv("OPENAI_API_KEY")),
        finnhub_api_key=_nonempty(os.getenv("FINNHUB_API_KEY")),
        kite_api_key=_nonempty(os.getenv("KITE_API_KEY")),
        kite_api_secret=_nonempty(os.getenv("KITE_API_SECRET")),
        kite_access_token=_nonempty(os.getenv("KITE_ACCESS_TOKEN")) or _session_access_token(),
        kite_redirect_url=redirect,
        selection=SelectionConfig(**{
            k: v for k, v in _section(raw, "selection").items()
            if k in SelectionConfig.__dataclass_fields__
        }),
        ai=AIConfig(**{
            k: (model if k == "model" else v)
            for k, v in {**_section(raw, "ai"), "model": model}.items()
            if k in AIConfig.__dataclass_fields__
        }),
        indicators=IndicatorConfig(**{
            k: v for k, v in _section(raw, "indicators").items()
            if k in IndicatorConfig.__dataclass_fields__
        }),
        backtest=BacktestConfig(**{
            k: v for k, v in _section(raw, "backtest").items()
            if k in BacktestConfig.__dataclass_fields__
        }),
        risk=RiskConfig(**{
            k: v for k, v in _section(raw, "risk").items()
            if k in RiskConfig.__dataclass_fields__
        }),
        auto_trade=AutoTradeConfig(**{
            k: v for k, v in _section(raw, "auto_trade").items()
            if k in AutoTradeConfig.__dataclass_fields__
        }),
        small_swing=SmallSwingConfig(**{
            k: v for k, v in _section(raw, "small_swing").items()
            if k in SmallSwingConfig.__dataclass_fields__
        }),
    )
