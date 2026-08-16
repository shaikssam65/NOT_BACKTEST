from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from trading_bot.ai_layer import get_ai_signal, heuristic_ai_signal
from trading_bot.config import Settings
from trading_bot.data_provider import HistoricalDataProvider, as_date
from trading_bot.indicators import add_indicators, snapshot_from_frame
from trading_bot.models import BacktestResult
from trading_bot.strategies import (
    VALID_STRATEGIES,
    final_signal,
    needs_ai,
    normalize_strategy,
    rule_signal_for,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def position_qty(capital: float, risk_pct: float, entry: float, stop: float) -> int:
    """qty = floor((capital * risk_per_trade_pct) / (entry - stop))."""
    risk_amount = capital * (risk_pct / 100.0)
    per_share = entry - stop
    if per_share <= 0 or risk_amount <= 0:
        return 0
    qty = math.floor(risk_amount / per_share)
    max_affordable = math.floor(capital / entry) if entry > 0 else 0
    return max(0, min(qty, max_affordable))


def _cache_key(
    symbol: str,
    strategy_name: str,
    start: date,
    end: date,
    capital: float,
    use_llm: bool,
    settings: Settings,
) -> str:
    payload = {
        "symbol": symbol.upper(),
        "strategy": strategy_name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "capital": capital,
        "use_llm": use_llm,
        "indicators": settings.indicators.__dict__,
        "backtest": settings.backtest.__dict__,
        "risk": settings.risk.__dict__,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_cache(conn, key: str) -> BacktestResult | None:
    row = conn.execute(
        "SELECT results_json FROM backtest_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if not row:
        return None
    data = json.loads(row["results_json"])
    result = BacktestResult(**data)
    result.cached = True
    return result


def _save_cache(conn, key: str, result: BacktestResult) -> None:
    payload = result.to_dict()
    payload["cached"] = False
    conn.execute(
        """
        INSERT INTO backtest_cache (
            cache_key, symbol, strategy_name, start_date, end_date, capital, results_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            results_json=excluded.results_json,
            created_at=excluded.created_at
        """,
        (
            key,
            result.symbol,
            result.strategy_name,
            result.start_date,
            result.end_date,
            result.capital,
            json.dumps(payload),
            _now_iso(),
        ),
    )
    conn.commit()


def _max_drawdown(curve: list[dict[str, Any]]) -> float:
    peak = float("-inf")
    max_dd = 0.0
    for point in curve:
        equity = float(point["equity"])
        peak = max(peak, equity)
        if peak <= 0:
            continue
        dd = (peak - equity) / peak * 100.0
        max_dd = max(max_dd, dd)
    return round(max_dd, 4)


def _signal_for_strategy(
    strategy: str,
    rule_signal: str,
    ai_signal: str,
) -> str:
    return final_signal(strategy, rule_signal, ai_signal)  # type: ignore[arg-type]


def _template_commentary(result: BacktestResult) -> str:
    if result.number_of_trades == 0:
        return (
            f"{result.symbol} / {result.strategy_name}: no trades between "
            f"{result.start_date} and {result.end_date}. The filter stayed out."
        )
    tone = "profitable" if result.total_return_pct >= 0 else "losing"
    return (
        f"{result.symbol} / {result.strategy_name} was {tone} over "
        f"{result.start_date} to {result.end_date}: {result.total_return_pct:.2f}% total return, "
        f"win rate {result.win_rate:.1f}% on {result.number_of_trades} trades, "
        f"max drawdown {result.max_drawdown_pct:.2f}%. "
        "This is a historical simulation, not a live result."
    )


def _llm_commentary(result: BacktestResult, settings: Settings) -> str:
    if not settings.openai_ready:
        return _template_commentary(result)
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key, timeout=settings.ai.timeout_seconds)
        payload = {
            "symbol": result.symbol,
            "strategy": result.strategy_name,
            "total_return_pct": result.total_return_pct,
            "win_rate": result.win_rate,
            "max_drawdown_pct": result.max_drawdown_pct,
            "trades": result.number_of_trades,
            "wins": result.wins,
            "losses": result.losses,
        }
        response = client.chat.completions.create(
            model=settings.ai.model,
            temperature=0.2,
            max_tokens=220,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Explain this NSE cash-equity backtest in 3 short sentences of plain language. "
                        "No advice, no emojis, mention that it is a simulation."
                    ),
                },
                {"role": "user", "content": json.dumps(payload)},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        return text or _template_commentary(result)
    except Exception:
        logger.exception("Backtest commentary LLM call failed")
        return _template_commentary(result)


def backtest(
    symbol: str,
    strategy_name: str,
    start_date: date | str,
    end_date: date | str,
    capital: float,
    settings: Settings,
    provider: HistoricalDataProvider,
    conn,
    *,
    use_llm: bool = False,
) -> BacktestResult:
    strategy_name = normalize_strategy(strategy_name)
    if strategy_name == "ensemble":
        raise ValueError(
            "ensemble is for daily auto-trade only. Use combined / trend_quality / *_ai for backtests."
        )
    if strategy_name not in VALID_STRATEGIES and strategy_name not in {
        "rule_based",
        "ai_filtered",
    }:
        # normalize_strategy already maps aliases; accept all known labels
        from trading_bot.strategies import STRATEGY_LABELS

        if strategy_name not in STRATEGY_LABELS:
            raise ValueError(f"Unknown strategy '{strategy_name}'. Choose from {VALID_STRATEGIES}")
    start = as_date(start_date)
    end = as_date(end_date)
    if end <= start:
        raise ValueError("end_date must be after start_date")
    if capital <= 0:
        raise ValueError("capital must be positive")

    key = _cache_key(symbol, strategy_name, start, end, capital, use_llm, settings)
    cached = _load_cache(conn, key)
    if cached is not None:
        return cached

    warmup = settings.indicators.sma_slow + 10
    fetch_start = date.fromordinal(start.toordinal() - warmup - 5)
    raw = provider.get_ohlcv(symbol.upper(), fetch_start, end)
    if raw.empty:
        raise RuntimeError(f"No historical data for {symbol}")
    df = add_indicators(raw, settings.indicators)
    df = df[(df["date"] >= pd.Timestamp(fetch_start)) & (df["date"] <= pd.Timestamp(end))]
    df = df.reset_index(drop=True)

    cash = float(capital)
    equity = float(capital)
    position: dict[str, Any] | None = None
    pending_entry: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    bt = settings.backtest
    risk_pct = settings.risk.risk_per_trade_pct
    slip = bt.slippage_pct / 100.0
    commission = bt.commission_pct / 100.0

    def mark_to_market(px: float) -> float:
        if position is None:
            return cash
        return cash + position["qty"] * px

    def close_position(bar, price: float, reason: str) -> None:
        nonlocal cash, position
        assert position is not None
        fill = float(price) * (1 - slip)
        proceeds = position["qty"] * fill
        fee = proceeds * commission
        pnl = proceeds - fee - position["cost"]
        cash += proceeds - fee
        ret = (fill - position["entry"]) / position["entry"] * 100.0
        trades.append(
            {
                "symbol": symbol.upper(),
                "entry_date": position["entry_date"],
                "exit_date": pd.Timestamp(bar.date).date().isoformat(),
                "entry_price": round(position["entry"], 4),
                "exit_price": round(fill, 4),
                "qty": position["qty"],
                "pnl": round(pnl, 2),
                "return_pct": round(ret, 4),
                "reason": reason,
                "stop_loss": round(position["stop"], 4),
                "target": round(position["target"], 4),
            }
        )
        position = None

    in_window = False
    for i, bar in enumerate(df.itertuples(index=False)):
        bar_date = pd.Timestamp(bar.date).date()
        if bar_date >= start:
            in_window = True

        # Fill a pending next-open entry before evaluating this bar's exits/signals.
        if pending_entry is not None and in_window and position is None:
            entry = float(bar.open) * (1 + slip)
            stop_pct = pending_entry["stop_pct"]
            target_pct = pending_entry["target_pct"]
            stop = entry * (1 - stop_pct / 100.0)
            target = entry * (1 + target_pct / 100.0)
            qty = position_qty(cash, risk_pct, entry, stop)
            cost = qty * entry
            fee = cost * commission
            if qty > 0 and cash >= cost + fee:
                cash -= cost + fee
                position = {
                    "qty": qty,
                    "entry": entry,
                    "stop": stop,
                    "target": target,
                    "cost": cost + fee,
                    "entry_date": bar_date.isoformat(),
                }
            pending_entry = None

        if position is not None and in_window:
            o, h, l, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
            stop = position["stop"]
            target = position["target"]
            exited = False
            if o <= stop:
                close_position(bar, o, "stop_gap")
                exited = True
            elif o >= target:
                close_position(bar, o, "target_gap")
                exited = True
            elif l <= stop:
                close_position(bar, stop, "stop_loss")
                exited = True
            elif h >= target:
                close_position(bar, target, "target")
                exited = True
            if not exited:
                bar_row = pd.Series(bar._asdict())
                score, rule_sig = rule_signal_for(strategy_name, bar_row)
                snap = snapshot_from_frame(df.iloc[: i + 1])
                if not needs_ai(strategy_name):
                    ai_signal = "buy"
                else:
                    if use_llm:
                        ai = get_ai_signal(
                            symbol.upper(),
                            df.iloc[: i + 1],
                            snap,
                            settings,
                            conn,
                            as_of_date=bar_date.isoformat(),
                            use_llm=True,
                        )
                    else:
                        ai = heuristic_ai_signal(snap, settings)
                    ai_signal = ai.signal
                action = _signal_for_strategy(strategy_name, rule_sig, ai_signal)
                if action == "avoid":
                    close_position(bar, c, "signal_exit")

        if in_window and position is None and pending_entry is None:
            bar_row = pd.Series(bar._asdict())
            score, rule_signal = rule_signal_for(strategy_name, bar_row)
            snap = snapshot_from_frame(df.iloc[: i + 1])
            stop_pct = bt.default_stop_loss_pct
            target_pct = bt.default_target_pct
            if snap.atr and snap.last_close:
                atr_pct = (snap.atr / snap.last_close) * 100 * bt.atr_stop_mult
                stop_pct = max(stop_pct, min(settings.risk.max_stop_loss_pct, atr_pct))
                target_pct = max(target_pct, stop_pct * 2.0)
            if not needs_ai(strategy_name):
                ai_signal = "buy"
            else:
                if use_llm:
                    ai = get_ai_signal(
                        symbol.upper(),
                        df.iloc[: i + 1],
                        snap,
                        settings,
                        conn,
                        as_of_date=bar_date.isoformat(),
                        use_llm=True,
                    )
                else:
                    ai = heuristic_ai_signal(snap, settings)
                ai_signal = ai.signal
                stop_pct = ai.stop_loss_pct
                target_pct = ai.target_pct
            action = _signal_for_strategy(strategy_name, rule_signal, ai_signal)
            if action == "buy":
                if bt.enter_on_next_open:
                    pending_entry = {"stop_pct": stop_pct, "target_pct": target_pct}
                else:
                    entry = float(bar.close) * (1 + slip)
                    stop = entry * (1 - stop_pct / 100.0)
                    target = entry * (1 + target_pct / 100.0)
                    qty = position_qty(cash, risk_pct, entry, stop)
                    cost = qty * entry
                    fee = cost * commission
                    if qty > 0 and cash >= cost + fee:
                        cash -= cost + fee
                        position = {
                            "qty": qty,
                            "entry": entry,
                            "stop": stop,
                            "target": target,
                            "cost": cost + fee,
                            "entry_date": bar_date.isoformat(),
                        }

        if in_window:
            last_px = float(bar.close)
            equity = mark_to_market(last_px)
            curve.append({"date": bar_date.isoformat(), "equity": round(equity, 2)})

    if position is not None and not df.empty:
        last = df.iloc[-1]
        close_position(last, float(last["close"]), "end_of_test")
        if curve:
            curve[-1]["equity"] = round(cash, 2)

    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    ending = curve[-1]["equity"] if curve else capital
    result = BacktestResult(
        symbol=symbol.upper(),
        strategy_name=strategy_name,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        capital=float(capital),
        ending_equity=round(float(ending), 2),
        total_return_pct=round((float(ending) - capital) / capital * 100.0, 4),
        win_rate=round((wins / len(trades) * 100.0) if trades else 0.0, 2),
        max_drawdown_pct=_max_drawdown(curve),
        number_of_trades=len(trades),
        wins=wins,
        losses=losses,
        equity_curve=curve,
        trades=trades,
    )
    result.commentary = _llm_commentary(result, settings)
    _save_cache(conn, key, result)
    return result
