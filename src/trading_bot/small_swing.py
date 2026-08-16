"""Under-₹50 weekly swing: research 2–3 established medium names, equal split, auto +30% exits."""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any, Callable

from trading_bot.config import Settings
from trading_bot.data_provider import HistoricalDataProvider
from trading_bot.execution import list_open_positions, manage_open_positions, place_buy
from trading_bot.indicators import add_indicators, snapshot_from_frame
from trading_bot.kite_auth import kite_client
from trading_bot.models import AISignal, Candidate
from trading_bot.selection import persist_selections
from trading_bot.strategies import rules_combo_vote
from trading_bot.universe import get_universe

logger = logging.getLogger(__name__)
ProgressCb = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def fetch_kite_holdings(settings: Settings) -> list[dict[str, Any]]:
    if not settings.kite_ready:
        return []
    try:
        kite = kite_client(settings)
        rows = kite.holdings() or []
    except Exception:
        logger.exception("kite.holdings failed")
        return []
    out: list[dict[str, Any]] = []
    for h in rows:
        qty = int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0)
        if qty <= 0:
            continue
        symbol = str(h.get("tradingsymbol") or "").upper()
        avg = float(h.get("average_price") or 0)
        ltp = float(h.get("last_price") or 0)
        if not symbol or avg <= 0:
            continue
        pnl_pct = ((ltp - avg) / avg) * 100.0 if ltp > 0 else 0.0
        out.append(
            {
                "source": "kite_holding",
                "symbol": symbol,
                "qty": qty,
                "entry_price": round(avg, 2),
                "ltp": round(ltp, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_rs": round((ltp - avg) * qty, 2),
            }
        )
    return out


def auto_take_profits(
    conn,
    settings: Settings,
    *,
    min_profit_pct: float | None = None,
    include_kite_holdings: bool = True,
) -> dict[str, Any]:
    """
    Daily 10:00 IST job: auto-sell bot positions (and optional Kite holdings)
    when unrealized profit ≥ threshold. No human approval.
    """
    threshold = float(
        min_profit_pct if min_profit_pct is not None else settings.small_swing.min_profit_sell_pct
    )
    bot_actions = manage_open_positions(conn, settings)
    kite_actions: list[dict[str, Any]] = []

    if include_kite_holdings and settings.kite_ready:
        for h in fetch_kite_holdings(settings):
            if h["pnl_pct"] + 1e-9 < threshold:
                continue
            symbol = h["symbol"]
            qty = int(h["qty"])
            ltp = float(h["ltp"])
            if settings.paper_mode:
                kite_actions.append(
                    {
                        "symbol": symbol,
                        "action": "paper_skip_kite_sell",
                        "pnl_pct": h["pnl_pct"],
                        "qty": qty,
                        "ltp": ltp,
                        "note": "PAPER_MODE: would sell Kite holding (set PAPER_MODE=false for live)",
                    }
                )
                continue
            try:
                kite = kite_client(settings)
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=kite.EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                    quantity=qty,
                    order_type=kite.ORDER_TYPE_MARKET,
                    product=kite.PRODUCT_CNC,
                )
                kite_actions.append(
                    {
                        "symbol": symbol,
                        "action": "sell_kite_profit",
                        "ok": True,
                        "broker_order_id": str(order_id),
                        "pnl_pct": h["pnl_pct"],
                        "qty": qty,
                        "ltp": ltp,
                    }
                )
            except Exception as exc:
                logger.exception("Auto Kite profit sell failed for %s", symbol)
                kite_actions.append(
                    {"symbol": symbol, "action": "sell_kite_failed", "ok": False, "reason": str(exc)}
                )

    return {
        "threshold_pct": threshold,
        "bot_actions": bot_actions,
        "kite_actions": kite_actions,
        "sold_bot": sum(1 for a in bot_actions if str(a.get("action", "")).startswith("sell")),
        "sold_kite": sum(1 for a in kite_actions if a.get("action") == "sell_kite_profit"),
    }


def _score_small_stock(row) -> tuple[float, str]:
    score, signal, _votes = rules_combo_vote(row, min_buys=2)
    if signal == "buy":
        return float(score), "buy"
    if signal == "hold" and score >= 55:
        return float(score) * 0.85, "buy"
    return float(score), signal


def run_small_swing_trade(
    conn,
    settings: Settings,
    provider: HistoricalDataProvider,
    *,
    as_of: date | None = None,
    trade_capital: float | None = None,
    pick_count: int | None = None,
    progress: ProgressCb | None = None,
    take_profits_first: bool = True,
) -> dict[str, Any]:
    """
    Weekly-style buy: best 2–3 established names under ₹50, split capital, +30% target.
    Profits auto-sold by daily 10:00 IST job (and here if take_profits_first).
    """
    report = progress or _noop
    as_of = as_of or date.today()
    cfg = settings.small_swing
    capital = float(trade_capital if trade_capital is not None else settings.capital)
    n_picks = int(pick_count if pick_count is not None else cfg.pick_count)
    n_picks = max(2, min(3, n_picks))
    mode = "paper" if settings.paper_mode else "live"
    activity: list[str] = []

    def log(msg: str) -> None:
        activity.append(msg)
        report(msg)
        logger.info(msg)

    profit_report: dict[str, Any] | None = None
    if take_profits_first:
        log(f"Step 0 — Auto-take profits at ≥{cfg.min_profit_sell_pct}%…")
        profit_report = auto_take_profits(conn, settings)
        log(
            f"  Bot sells: {profit_report['sold_bot']} · "
            f"Kite sells: {profit_report['sold_kite']}"
        )

    log(
        f"Start under-₹{cfg.max_price:.0f} weekly buy · capital ₹{capital:,.0f} · "
        f"picks={n_picks} · target +{cfg.target_pct}% · auto-sell (no approval)"
    )
    log("Step 1 — Research medium established names under ₹50…")

    universe = get_universe(conn)
    lookback_start = as_of - timedelta(days=settings.selection.lookback_days + 20)
    scored: list[Candidate] = []
    scanned = 0
    skipped = 0

    for stock in universe:
        rank = int(stock.market_cap_rank)
        if rank < cfg.prefer_rank_above or rank > cfg.prefer_rank_below:
            skipped += 1
            continue
        price_hint = stock.last_price
        if price_hint is not None and price_hint > cfg.max_price:
            skipped += 1
            continue
        df = provider.get_ohlcv(stock.symbol, lookback_start, as_of)
        if df.empty or len(df) < settings.indicators.sma_slow + 5:
            skipped += 1
            continue
        indicated = add_indicators(df, settings.indicators)
        row = indicated.iloc[-1]
        last = float(row["close"])
        if last > cfg.max_price or last <= 0:
            skipped += 1
            continue
        scanned += 1
        score, signal = _score_small_stock(row)
        if signal != "buy":
            continue
        snap = snapshot_from_frame(indicated)
        snap.rule_score = int(round(score))
        snap.rule_signal = "buy"
        mid = (cfg.prefer_rank_above + cfg.prefer_rank_below) / 2
        rank_boost = max(0.0, 10.0 - abs(rank - mid) / 10.0)
        combined = score + rank_boost + max(0.0, (cfg.max_price - last) / cfg.max_price * 8)
        ai = AISignal(
            signal="buy",
            confidence=int(min(95, round(score))),
            stop_loss_pct=cfg.stop_loss_pct,
            target_pct=cfg.target_pct,
            reasoning=f"Under-₹{cfg.max_price:.0f} established (rank {rank}) @ ₹{last:.2f}",
            source="small_swing",
        )
        scored.append(
            Candidate(
                stock=stock,
                indicators=snap,
                ai=ai,
                combined_signal="buy",
                combined_score=combined,
                source="ai_selected",
            )
        )
        log(f"  Candidate {stock.symbol} @ {last:.2f} rank={rank} score={combined:.1f}")

    scored.sort(key=lambda c: c.combined_score, reverse=True)
    picks = scored[:n_picks]
    persist_selections(conn, as_of.isoformat(), picks)
    log(f"Step 2 — Selected {len(picks)}: {[p.symbol for p in picks] or ['none']}")

    if not picks:
        return {
            "date": as_of.isoformat(),
            "mode": mode,
            "strategy": "small_swing",
            "style": "under50_weekly",
            "paper_mode": settings.paper_mode,
            "trade_capital": capital,
            "scanned": scanned,
            "skipped_data": skipped,
            "detected": [],
            "picks": [],
            "orders": [],
            "entry_plans": [],
            "profit_exits": profit_report,
            "activity": activity,
            "open_positions": list_open_positions(conn),
            "note": "No under-₹50 buys this run. Daily 10:00 IST job still auto-sells at +30%.",
        }

    slice_cap = capital / len(picks)
    log(f"Step 3 — Split ₹{capital:,.0f} → ₹{slice_cap:,.0f} each")

    order_results: list[dict[str, Any]] = []
    entry_plans: list[dict[str, Any]] = []
    detected: list[dict[str, Any]] = []

    for pick in picks:
        last = pick.last_price
        stop = last * (1 - cfg.stop_loss_pct / 100.0)
        target = last * (1 + cfg.target_pct / 100.0)
        qty = max(0, math.floor(slice_cap / last))
        detected.append(
            {
                "symbol": pick.symbol,
                "price": round(last, 2),
                "rank": pick.stock.market_cap_rank,
                "score": pick.combined_score,
                "slice_capital": round(slice_cap, 2),
                "planned_qty": qty,
                "target_pct": cfg.target_pct,
                "reasoning": pick.ai.reasoning,
            }
        )
        if qty <= 0:
            order_results.append(
                {"symbol": pick.symbol, "ok": False, "reason": "qty_zero_for_slice", "mode": mode}
            )
            continue
        result = place_buy(
            conn,
            settings,
            symbol=pick.symbol,
            entry_price=last,
            stop_loss=stop,
            target=target,
            strategy="small_swing",
            source="ai_selected",
            available_capital=slice_cap,
            qty=qty,
        )
        order_results.append(
            {
                "symbol": pick.symbol,
                "entry_price": round(last, 2),
                "stop_loss": round(stop, 2),
                "target": round(target, 2),
                "slice_capital": round(slice_cap, 2),
                **result,
            }
        )
        if result.get("ok"):
            entry_plans.append(
                {
                    "symbol": pick.symbol,
                    "entry": round(last, 2),
                    "qty": result.get("qty"),
                    "stop_loss": round(stop, 2),
                    "target": round(target, 2),
                    "style": "under50_weekly",
                    "exit_when": (
                        f"AUTO-SELL at +{cfg.min_profit_sell_pct:.0f}% (₹{target:.2f}) "
                        f"or stop ₹{stop:.2f}. Daily 10:00 IST job — no approval."
                    ),
                }
            )
            log(
                f"  ORDER {pick.symbol}: BUY {result.get('qty')} @ {last:.2f} "
                f"| Target +{cfg.target_pct}% @ {target:.2f}"
            )
        else:
            log(f"  ORDER REJECTED {pick.symbol}: {result.get('reason')}")

    return {
        "date": as_of.isoformat(),
        "mode": mode,
        "strategy": "small_swing",
        "style": "under50_weekly",
        "paper_mode": settings.paper_mode,
        "trade_capital": capital,
        "slice_capital": slice_cap,
        "scanned": scanned,
        "skipped_data": skipped,
        "detected": detected,
        "picks": [p.symbol for p in picks],
        "orders": order_results,
        "entry_plans": entry_plans,
        "profit_exits": profit_report,
        "activity": activity,
        "open_positions": list_open_positions(conn),
        "note": (
            f"Under-₹{cfg.max_price:.0f} · split capital · auto-sell at "
            f"+{cfg.min_profit_sell_pct:.0f}% (schedule daily 10:00 IST). No approval."
        ),
    }
