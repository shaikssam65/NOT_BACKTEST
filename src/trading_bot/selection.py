from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from trading_bot.ai_layer import combine_signals, combined_score, get_ai_signal
from trading_bot.config import Settings
from trading_bot.data_provider import HistoricalDataProvider, as_date
from trading_bot.indicators import add_indicators, snapshot_from_frame
from trading_bot.models import Candidate, SelectionSource, UniverseStock
from trading_bot.universe import get_stock, get_universe

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def apply_selection_constraints(
    candidates: list[Candidate],
    *,
    max_picks: int,
    min_top100: int,
    max_below_price: int,
    below_price_threshold: float,
) -> list[Candidate]:
    """Greedy pick of the highest-scoring names that still satisfy universe rules."""
    ranked = sorted(candidates, key=lambda c: c.combined_score, reverse=True)
    picked: list[Candidate] = []

    def cheap_count(items: list[Candidate]) -> int:
        return sum(1 for item in items if item.last_price < below_price_threshold)

    def can_add(item: Candidate) -> bool:
        if any(existing.symbol == item.symbol for existing in picked):
            return False
        if item.last_price < below_price_threshold and cheap_count(picked) >= max_below_price:
            return False
        return True

    top100 = [c for c in ranked if c.stock.in_top100]
    for item in top100:
        if sum(1 for p in picked if p.stock.in_top100) >= min_top100:
            break
        if len(picked) >= max_picks:
            break
        if can_add(item):
            picked.append(item)

    for item in ranked:
        if len(picked) >= max_picks:
            break
        if can_add(item):
            picked.append(item)

    return picked


def _evaluate_symbol(
    stock: UniverseStock,
    as_of: date,
    settings: Settings,
    provider: HistoricalDataProvider,
    conn,
    *,
    use_llm: bool,
    source: SelectionSource,
) -> Candidate | None:
    start = as_of.fromordinal(as_of.toordinal() - settings.selection.lookback_days - 20)
    df = provider.get_ohlcv(stock.symbol, start, as_of)
    if df.empty or len(df) < settings.indicators.sma_slow + 5:
        logger.warning("Skipping %s: not enough history", stock.symbol)
        return None
    indicated = add_indicators(df, settings.indicators)
    snap = snapshot_from_frame(indicated)
    ai = get_ai_signal(
        stock.symbol,
        indicated,
        snap,
        settings,
        conn,
        as_of_date=as_of.isoformat(),
        use_llm=use_llm,
    )
    combined = combine_signals(snap.rule_signal, ai.signal)
    return Candidate(
        stock=stock,
        indicators=snap,
        ai=ai,
        combined_signal=combined,
        combined_score=combined_score(snap.rule_score, ai, combined),
        source=source,
    )


def persist_selections(conn, selection_date: str, picks: list[Candidate]) -> None:
    conn.execute("DELETE FROM daily_selections WHERE selection_date = ?", (selection_date,))
    for pick in picks:
        record = pick.to_record(selection_date)
        conn.execute(
            """
            INSERT INTO daily_selections (
                selection_date, symbol, source, rule_signal, ai_signal, combined_signal,
                confidence, entry_price_target, stop_loss_pct, target_pct, reasoning, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["selection_date"],
                record["symbol"],
                record["source"],
                record["rule_signal"],
                record["ai_signal"],
                record["combined_signal"],
                record["confidence"],
                record["entry_price_target"],
                record["stop_loss_pct"],
                record["target_pct"],
                record["reasoning"],
                _now_iso(),
            ),
        )
    conn.commit()


def run_daily_selection(
    conn,
    settings: Settings,
    provider: HistoricalDataProvider,
    *,
    as_of: date | str | None = None,
    manual_symbol: str | None = None,
    use_llm: bool = True,
) -> list[Candidate]:
    as_of_date = as_date(as_of or date.today())
    universe = get_universe(conn)
    if not universe:
        raise RuntimeError("Universe is empty. Run refresh-universe first.")

    scored: list[Candidate] = []
    # Score the whole universe with rules first (cheap), then LLM the top pool.
    rule_ranked: list[tuple[UniverseStock, object]] = []
    lookback_start = as_of_date.fromordinal(
        as_of_date.toordinal() - settings.selection.lookback_days - 20
    )
    for stock in universe:
        df = provider.get_ohlcv(stock.symbol, lookback_start, as_of_date)
        if df.empty or len(df) < settings.indicators.sma_slow + 5:
            continue
        indicated = add_indicators(df, settings.indicators)
        snap = snapshot_from_frame(indicated)
        rule_ranked.append((stock, snap, indicated))

    rule_ranked.sort(key=lambda item: item[1].rule_score, reverse=True)
    pool = rule_ranked[: settings.selection.candidate_pool]
    logger.info("AI pool size %s / %s scored names", len(pool), len(rule_ranked))

    for stock, snap, indicated in pool:
        ai = get_ai_signal(
            stock.symbol,
            indicated,
            snap,
            settings,
            conn,
            as_of_date=as_of_date.isoformat(),
            use_llm=use_llm,
        )
        combined = combine_signals(snap.rule_signal, ai.signal)
        scored.append(
            Candidate(
                stock=stock,
                indicators=snap,
                ai=ai,
                combined_signal=combined,
                combined_score=combined_score(snap.rule_score, ai, combined),
                source="ai_selected",
            )
        )

    buyable = [c for c in scored if c.combined_signal == "buy"]
    if len(buyable) < settings.selection.min_from_top100:
        logger.warning(
            "Only %s combined-buy names; constraints may yield fewer than 3-4 picks",
            len(buyable),
        )

    picks = apply_selection_constraints(
        buyable,
        max_picks=settings.selection.pick_count_max,
        min_top100=settings.selection.min_from_top100,
        max_below_price=settings.selection.max_below_price,
        below_price_threshold=settings.selection.below_price_threshold,
    )

    if manual_symbol:
        manual = get_stock(conn, manual_symbol) or UniverseStock(
            symbol=manual_symbol.upper(),
            name=manual_symbol.upper(),
            market_cap_rank=999,
        )
        evaluated = _evaluate_symbol(
            manual,
            as_of_date,
            settings,
            provider,
            conn,
            use_llm=use_llm,
            source="manual",
        )
        if evaluated is None:
            logger.warning("Manual symbol %s could not be evaluated", manual_symbol)
        else:
            evaluated.source = "manual"
            picks = [p for p in picks if p.symbol != evaluated.symbol] + [evaluated]
            logger.info(
                "Manual override added %s (combined=%s). Risk gate still applies before any order.",
                evaluated.symbol,
                evaluated.combined_signal,
            )

    persist_selections(conn, as_of_date.isoformat(), picks)
    return picks


def list_selections(conn, selection_date: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT selection_date, symbol, source, rule_signal, ai_signal, combined_signal,
               confidence, entry_price_target, stop_loss_pct, target_pct, reasoning, created_at
        FROM daily_selections
        WHERE selection_date = ?
        ORDER BY CASE source WHEN 'ai_selected' THEN 0 ELSE 1 END, symbol
        """,
        (selection_date,),
    ).fetchall()
    return [dict(row) for row in rows]
