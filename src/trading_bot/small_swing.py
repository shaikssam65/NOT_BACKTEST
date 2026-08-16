"""Small-stock swing mode: 2–3 cheap names, equal capital split, +30% target.

Sells at ≥30% profit require human approval (bot book + Kite holdings).
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any, Callable

from trading_bot.config import Settings
from trading_bot.data_provider import HistoricalDataProvider
from trading_bot.execution import list_open_positions, place_buy, place_sell
from trading_bot.indicators import add_indicators, snapshot_from_frame
from trading_bot.kite_auth import fetch_ltp, kite_client
from trading_bot.models import AISignal, Candidate
from trading_bot.selection import persist_selections
from trading_bot.strategies import rules_combo_vote
from trading_bot.universe import get_universe

logger = logging.getLogger(__name__)
ProgressCb = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def fetch_kite_holdings(settings: Settings) -> list[dict[str, Any]]:
    """Pull CNC holdings from the linked Zerodha account (empty if not connected)."""
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
                "exchange": h.get("exchange") or "NSE",
                "product": h.get("product") or "CNC",
                "position_id": None,
            }
        )
    return out


def propose_profit_exits(
    conn,
    settings: Settings,
    *,
    min_profit_pct: float | None = None,
) -> list[dict[str, Any]]:
    """
    Candidates with unrealized profit ≥ threshold.
    Includes bot open positions and (if connected) Kite holdings.
    Does NOT sell — human must approve.
    """
    cfg = settings.small_swing
    threshold = float(min_profit_pct if min_profit_pct is not None else cfg.min_profit_sell_pct)
    proposals: list[dict[str, Any]] = []

    opens = list_open_positions(conn)
    symbols = [p["symbol"] for p in opens]
    prices: dict[str, float] = {}
    try:
        if settings.kite_ready and symbols:
            prices = fetch_ltp(symbols, settings)
    except Exception:
        logger.exception("LTP for bot positions failed")

    for pos in opens:
        sym = pos["symbol"]
        entry = float(pos["entry_price"])
        qty = int(pos["qty"])
        ltp = prices.get(sym)
        if ltp is None:
            # Fall back to target as proxy only for messaging — skip if no price
            continue
        pnl_pct = ((ltp - entry) / entry) * 100.0 if entry > 0 else 0.0
        if pnl_pct + 1e-9 < threshold:
            continue
        proposals.append(
            {
                "source": "bot_position",
                "symbol": sym,
                "qty": qty,
                "entry_price": round(entry, 2),
                "ltp": round(ltp, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_rs": round((ltp - entry) * qty, 2),
                "position_id": pos["id"],
                "strategy": pos.get("strategy"),
                "needs_approval": True,
                "reason": f"Unrealized +{pnl_pct:.1f}% ≥ {threshold}% — approve to sell",
            }
        )

    for h in fetch_kite_holdings(settings):
        if h["pnl_pct"] + 1e-9 < threshold:
            continue
        # Skip if already listed as bot position for same symbol
        if any(p["symbol"] == h["symbol"] and p["source"] == "bot_position" for p in proposals):
            continue
        proposals.append(
            {
                **h,
                "needs_approval": True,
                "reason": f"Kite holding +{h['pnl_pct']:.1f}% ≥ {threshold}% — approve to sell",
            }
        )

    proposals.sort(key=lambda x: x["pnl_pct"], reverse=True)
    return proposals


def execute_approved_sells(
    conn,
    settings: Settings,
    approvals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Execute sells the user approved.
    - bot_position: place_sell on our book
    - kite_holding: live sell only if PAPER_MODE=false; paper logs intent only
    """
    results: list[dict[str, Any]] = []
    for item in approvals:
        symbol = str(item.get("symbol") or "").upper()
        source = item.get("source")
        ltp = float(item.get("ltp") or 0)
        qty = int(item.get("qty") or 0)
        if not symbol or qty <= 0 or ltp <= 0:
            results.append({"symbol": symbol, "ok": False, "reason": "invalid_row"})
            continue

        if source == "bot_position":
            pid = item.get("position_id")
            if pid is None:
                results.append({"symbol": symbol, "ok": False, "reason": "missing_position_id"})
                continue
            result = place_sell(
                conn,
                settings,
                position_id=int(pid),
                exit_price=ltp,
                reason="approved_profit_take",
            )
            results.append({"symbol": symbol, "source": source, **result})
            continue

        if source == "kite_holding":
            if settings.paper_mode:
                results.append(
                    {
                        "symbol": symbol,
                        "source": source,
                        "ok": True,
                        "mode": "paper",
                        "reason": "paper_skip_real_holding_sell",
                        "note": (
                            f"PAPER: would sell {qty} {symbol} @ {ltp} "
                            "(turn PAPER_MODE=false for real Kite sell)"
                        ),
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
                results.append(
                    {
                        "symbol": symbol,
                        "source": source,
                        "ok": True,
                        "mode": "live",
                        "broker_order_id": str(order_id),
                        "qty": qty,
                        "price": ltp,
                    }
                )
            except Exception as exc:
                logger.exception("Approved Kite sell failed for %s", symbol)
                results.append({"symbol": symbol, "source": source, "ok": False, "reason": str(exc)})
            continue

        results.append({"symbol": symbol, "ok": False, "reason": f"unknown_source_{source}"})
    return results


def _score_small_stock(row) -> tuple[float, str]:
    score, signal, _votes = rules_combo_vote(row, min_buys=2)
    # Soften threshold for small names: allow hold with decent score as candidate pool
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
) -> dict[str, Any]:
    """
    Find best 2–3 small stocks, split capital equally, buy with +30% target.
    Does not auto-sell profits — those go through propose_profit_exits + approval.
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

    log(f"Start small_swing · capital ₹{capital:,.0f} · picks={n_picks} · target +{cfg.target_pct}%")
    log("Step 1 — Scan for small stocks (price filter + rules score)…")

    universe = get_universe(conn)
    lookback_start = as_of - timedelta(days=settings.selection.lookback_days + 20)
    scored: list[Candidate] = []
    scanned = 0
    skipped = 0

    for stock in universe:
        # Prefer smaller / cheaper names
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
        # Boost smaller names slightly
        rank_boost = 5.0 if stock.market_cap_rank >= cfg.prefer_rank_above else 0.0
        combined = score + rank_boost + max(0.0, (cfg.max_price - last) / cfg.max_price * 10)
        ai = AISignal(
            signal="buy",
            confidence=int(min(95, round(score))),
            stop_loss_pct=cfg.stop_loss_pct,
            target_pct=cfg.target_pct,
            reasoning=f"Small-swing candidate @ ₹{last:.2f}, score {score:.0f}",
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
        log(f"  Candidate {stock.symbol} @ {last:.2f} score={combined:.1f}")

    scored.sort(key=lambda c: c.combined_score, reverse=True)
    picks = scored[:n_picks]
    persist_selections(conn, as_of.isoformat(), picks)
    log(f"Step 2 — Selected {len(picks)}: {[p.symbol for p in picks] or ['none']}")

    if not picks:
        proposals = propose_profit_exits(conn, settings)
        return {
            "date": as_of.isoformat(),
            "mode": mode,
            "strategy": "small_swing",
            "style": "small_swing_30",
            "paper_mode": settings.paper_mode,
            "trade_capital": capital,
            "scanned": scanned,
            "skipped_data": skipped,
            "detected": [],
            "picks": [],
            "orders": [],
            "entry_plans": [],
            "profit_proposals": proposals,
            "activity": activity,
            "open_positions": list_open_positions(conn),
            "note": "No small-stock buys today. Check profit proposals below for 30%+ sells (need your approval).",
        }

    slice_cap = capital / len(picks)
    log(f"Step 3 — Split ₹{capital:,.0f} → ₹{slice_cap:,.0f} each · target +{cfg.target_pct}%")

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
                "score": pick.combined_score,
                "slice_capital": round(slice_cap, 2),
                "planned_qty": qty,
                "stop_pct": cfg.stop_loss_pct,
                "target_pct": cfg.target_pct,
                "reasoning": pick.ai.reasoning,
            }
        )
        if qty <= 0:
            order_results.append(
                {"symbol": pick.symbol, "ok": False, "reason": "qty_zero_for_slice", "mode": mode}
            )
            log(f"  SKIP {pick.symbol}: price too high for slice ₹{slice_cap:,.0f}")
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
        row_out = {
            "symbol": pick.symbol,
            "entry_price": round(last, 2),
            "stop_loss": round(stop, 2),
            "target": round(target, 2),
            "slice_capital": round(slice_cap, 2),
            **result,
        }
        order_results.append(row_out)
        if result.get("ok"):
            entry_plans.append(
                {
                    "symbol": pick.symbol,
                    "entry": round(last, 2),
                    "qty": result.get("qty"),
                    "stop_loss": round(stop, 2),
                    "target": round(target, 2),
                    "style": "small_swing",
                    "exit_when": (
                        f"Target +{cfg.target_pct}% at ₹{target:.2f}. "
                        f"When profit ≥ {cfg.min_profit_sell_pct}%, approve sell in UI "
                        "(not auto-sold). Stop at ₹{stop:.2f} can auto-trigger on exit check."
                    ),
                }
            )
            log(
                f"  ORDER {pick.symbol}: BUY {result.get('qty')} @ {last:.2f} "
                f"| SL {stop:.2f} | Target {target:.2f} (+{cfg.target_pct}%)"
            )
        else:
            log(f"  ORDER REJECTED {pick.symbol}: {result.get('reason')}")

    log("Step 4 — Scan for ≥30% profit positions needing your approval…")
    proposals = propose_profit_exits(conn, settings)
    log(f"  Profit proposals awaiting approval: {len(proposals)}")

    return {
        "date": as_of.isoformat(),
        "mode": mode,
        "strategy": "small_swing",
        "style": "small_swing_30",
        "paper_mode": settings.paper_mode,
        "trade_capital": capital,
        "slice_capital": slice_cap,
        "scanned": scanned,
        "skipped_data": skipped,
        "detected": detected,
        "picks": [p.symbol for p in picks],
        "orders": order_results,
        "entry_plans": entry_plans,
        "profit_proposals": proposals,
        "activity": activity,
        "open_positions": list_open_positions(conn),
        "note": (
            f"Small-swing PAPER · equal split · +{cfg.target_pct}% target. "
            "Sells at 30%+ need your approval below."
            if settings.paper_mode
            else f"Small-swing LIVE · +{cfg.target_pct}% target · approval required for profit sells."
        ),
    }
