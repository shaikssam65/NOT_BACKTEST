"""Daily automated trading loop — PAPER_MODE by default."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from trading_bot.ai_layer import get_ai_signal, heuristic_ai_signal
from trading_bot.config import Settings
from trading_bot.data_provider import HistoricalDataProvider
from trading_bot.execution import list_open_positions, manage_open_positions, place_buy
from trading_bot.indicators import add_indicators, snapshot_from_frame
from trading_bot.selection import apply_selection_constraints, persist_selections
from trading_bot.strategies import final_signal, needs_ai, normalize_strategy, rule_signal_for
from trading_bot.models import Candidate
from trading_bot.universe import get_universe

logger = logging.getLogger(__name__)


def _stop_target_from_ai_or_defaults(ai, settings: Settings, last: float) -> tuple[float, float]:
    stop_pct = float(ai.stop_loss_pct if ai else settings.backtest.default_stop_loss_pct)
    target_pct = float(ai.target_pct if ai else settings.backtest.default_target_pct)
    stop = last * (1 - stop_pct / 100.0)
    target = last * (1 + target_pct / 100.0)
    return stop, target


def run_daily_auto_trade(
    conn,
    settings: Settings,
    provider: HistoricalDataProvider,
    *,
    strategy: str = "combined",
    as_of: date | None = None,
    use_llm: bool = True,
    manage_exits: bool = True,
) -> dict[str, Any]:
    """
    1) Manage open positions (SL/target vs live LTP when Kite connected)
    2) Select buys with the chosen strategy (+ AI filter when required)
    3) Place paper (or live if PAPER_MODE=false) orders through the risk gate
    """
    strategy = normalize_strategy(strategy)
    as_of = as_of or date.today()
    mode = "paper" if settings.paper_mode else "live"

    exit_actions: list[dict[str, Any]] = []
    if manage_exits:
        exit_actions = manage_open_positions(conn, settings)

    universe = get_universe(conn)
    lookback_start = as_of - timedelta(days=settings.selection.lookback_days + 20)
    scored: list[Candidate] = []

    for stock in universe:
        df = provider.get_ohlcv(stock.symbol, lookback_start, as_of)
        if df.empty or len(df) < settings.indicators.sma_slow + 5:
            continue
        indicated = add_indicators(df, settings.indicators)
        row = indicated.iloc[-1]
        score, rule_sig = rule_signal_for(strategy, row)
        snap = snapshot_from_frame(indicated)
        # Override snapshot rule fields with strategy-specific signal
        snap.rule_score = score
        snap.rule_signal = rule_sig

        if needs_ai(strategy):
            ai = get_ai_signal(
                stock.symbol,
                indicated,
                snap,
                settings,
                conn,
                as_of_date=as_of.isoformat(),
                use_llm=bool(use_llm and settings.openai_ready),
            )
        else:
            ai = heuristic_ai_signal(snap, settings)

        combined = final_signal(strategy, rule_sig, ai.signal)
        from trading_bot.ai_layer import combined_score

        scored.append(
            Candidate(
                stock=stock,
                indicators=snap,
                ai=ai,
                combined_signal=combined,
                combined_score=combined_score(score, ai, combined),
                source="ai_selected",
            )
        )

    buyable = [c for c in scored if c.combined_signal == "buy"]
    buyable.sort(key=lambda c: c.combined_score, reverse=True)
    picks = apply_selection_constraints(
        buyable,
        max_picks=settings.selection.pick_count_max,
        min_top100=settings.selection.min_from_top100,
        max_below_price=settings.selection.max_below_price,
        below_price_threshold=settings.selection.below_price_threshold,
    )
    persist_selections(conn, as_of.isoformat(), picks)

    order_results: list[dict[str, Any]] = []
    for pick in picks:
        last = pick.last_price
        stop, target = _stop_target_from_ai_or_defaults(pick.ai, settings, last)
        result = place_buy(
            conn,
            settings,
            symbol=pick.symbol,
            entry_price=last,
            stop_loss=stop,
            target=target,
            strategy=strategy,
            source=pick.source,
        )
        order_results.append({"symbol": pick.symbol, **result})

    return {
        "date": as_of.isoformat(),
        "mode": mode,
        "strategy": strategy,
        "paper_mode": settings.paper_mode,
        "picks": [p.symbol for p in picks],
        "orders": order_results,
        "exits": exit_actions,
        "open_positions": list_open_positions(conn),
        "note": (
            "PAPER fills only — no real money."
            if settings.paper_mode
            else "LIVE mode — real orders may be sent to Zerodha."
        ),
    }
