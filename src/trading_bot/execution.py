"""Order execution — paper by default; live only when PAPER_MODE=false."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from trading_bot.config import Settings
from trading_bot.kite_auth import fetch_ltp, kite_client
from trading_bot.risk import OrderIntent, validate_order

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _mode(settings: Settings) -> str:
    return "paper" if settings.paper_mode else "live"


def _log_order(
    conn,
    *,
    symbol: str,
    side: str,
    qty: int,
    price: float | None,
    stop_loss: float | None,
    target: float | None,
    status: str,
    mode: str,
    reason: str,
    risk_check: str,
    broker_order_id: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO orders (
            created_at, symbol, side, qty, price, stop_loss, target,
            status, mode, reason, risk_check, broker_order_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now_iso(),
            symbol.upper(),
            side,
            qty,
            price,
            stop_loss,
            target,
            status,
            mode,
            reason,
            risk_check,
            broker_order_id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def place_buy(
    conn,
    settings: Settings,
    *,
    symbol: str,
    entry_price: float,
    stop_loss: float,
    target: float | None,
    strategy: str,
    source: str = "ai_selected",
    available_capital: float | None = None,
) -> dict[str, Any]:
    mode = _mode(settings)
    intent = OrderIntent(
        symbol=symbol,
        side="BUY",
        entry_price=entry_price,
        stop_loss_price=stop_loss,
        target_price=target,
        source=source,
    )
    decision = validate_order(
        intent,
        settings,
        conn,
        available_capital=available_capital,
    )
    if not decision.passed:
        _log_order(
            conn,
            symbol=symbol,
            side="BUY",
            qty=0,
            price=entry_price,
            stop_loss=stop_loss,
            target=target,
            status="rejected",
            mode=mode,
            reason=decision.reason,
            risk_check=json.dumps(decision.to_dict()),
        )
        return {"ok": False, "reason": decision.reason, "mode": mode}

    qty = decision.qty
    broker_id = None
    fill_price = entry_price

    if mode == "live":
        # Live path — only when PAPER_MODE=false explicitly.
        try:
            kite = kite_client(settings)
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NSE,
                tradingsymbol=symbol.upper(),
                transaction_type=kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_CNC,
            )
            broker_id = str(order_id)
            status = "submitted"
            reason = "live_order_submitted"
        except Exception as exc:
            logger.exception("Live BUY failed for %s", symbol)
            _log_order(
                conn,
                symbol=symbol,
                side="BUY",
                qty=qty,
                price=entry_price,
                stop_loss=stop_loss,
                target=target,
                status="rejected",
                mode=mode,
                reason=f"live_error:{exc}",
                risk_check=json.dumps(decision.to_dict()),
            )
            return {"ok": False, "reason": str(exc), "mode": mode}
    else:
        status = "filled"
        reason = "paper_fill"

    order_id = _log_order(
        conn,
        symbol=symbol,
        side="BUY",
        qty=qty,
        price=fill_price,
        stop_loss=stop_loss,
        target=target,
        status=status,
        mode=mode,
        reason=reason,
        risk_check=json.dumps(decision.to_dict()),
        broker_order_id=broker_id,
    )

    if mode == "paper" or (mode == "live" and broker_id):
        conn.execute(
            """
            INSERT INTO positions (
                opened_at, symbol, qty, entry_price, stop_loss, target,
                status, mode, strategy, source, broker_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                _now_iso(),
                symbol.upper(),
                qty,
                fill_price,
                stop_loss,
                target,
                mode,
                strategy,
                source,
                broker_id,
            ),
        )
        conn.commit()

    return {
        "ok": True,
        "mode": mode,
        "qty": qty,
        "price": fill_price,
        "order_id": order_id,
        "broker_order_id": broker_id,
        "stop_loss": stop_loss,
        "target": target,
    }


def place_sell(
    conn,
    settings: Settings,
    *,
    position_id: int,
    exit_price: float,
    reason: str,
) -> dict[str, Any]:
    mode = _mode(settings)
    row = conn.execute(
        "SELECT * FROM positions WHERE id = ? AND status = 'open'",
        (position_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "reason": "position_not_found"}

    symbol = row["symbol"]
    qty = int(row["qty"])
    entry = float(row["entry_price"])
    intent = OrderIntent(
        symbol=symbol,
        side="SELL",
        entry_price=exit_price,
        stop_loss_price=None,
        qty=qty,
    )
    decision = validate_order(intent, settings, conn)
    if not decision.passed:
        return {"ok": False, "reason": decision.reason}

    broker_id = None
    if mode == "live":
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
            broker_id = str(order_id)
        except Exception as exc:
            logger.exception("Live SELL failed for %s", symbol)
            return {"ok": False, "reason": str(exc)}

    pnl = (exit_price - entry) * qty
    conn.execute(
        """
        UPDATE positions
        SET status = 'closed', exit_time = ?, exit_price = ?, pnl = ?,
            exit_reason = ?, broker_exit_order_id = ?
        WHERE id = ?
        """,
        (_now_iso(), exit_price, round(pnl, 2), reason, broker_id, position_id),
    )
    _log_order(
        conn,
        symbol=symbol,
        side="SELL",
        qty=qty,
        price=exit_price,
        stop_loss=float(row["stop_loss"]),
        target=row["target"],
        status="filled" if mode == "paper" else "submitted",
        mode=mode,
        reason=reason,
        risk_check=json.dumps(decision.to_dict()),
        broker_order_id=broker_id,
    )
    return {"ok": True, "pnl": round(pnl, 2), "mode": mode, "broker_order_id": broker_id}


def list_open_positions(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'open' ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def manage_open_positions(
    conn,
    settings: Settings,
    *,
    as_of: date | None = None,
    fallback_prices: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Exit only on stop-loss or target — never force-sell just because a day passed."""
    as_of = as_of or date.today()
    opens = list_open_positions(conn)
    if not opens:
        return []
    symbols = [p["symbol"] for p in opens]
    try:
        prices = fetch_ltp(symbols, settings) if settings.kite_ready else {}
    except Exception:
        logger.exception("LTP fetch failed during position manage")
        prices = {}
    if fallback_prices:
        for sym, px in fallback_prices.items():
            prices.setdefault(sym.upper(), float(px))

    actions: list[dict[str, Any]] = []
    for pos in opens:
        symbol = pos["symbol"]
        ltp = prices.get(symbol)
        stop = float(pos["stop_loss"])
        target = pos["target"]
        if ltp is None:
            actions.append({"symbol": symbol, "action": "skip", "reason": "no_ltp"})
            continue
        if ltp <= stop:
            result = place_sell(conn, settings, position_id=pos["id"], exit_price=ltp, reason="stop_loss")
            actions.append({"symbol": symbol, "action": "sell_stop", **result, "ltp": ltp})
        elif target is not None and ltp >= float(target):
            result = place_sell(conn, settings, position_id=pos["id"], exit_price=ltp, reason="target")
            actions.append({"symbol": symbol, "action": "sell_target", **result, "ltp": ltp})
        else:
            actions.append(
                {
                    "symbol": symbol,
                    "action": "hold",
                    "ltp": ltp,
                    "note": "Waiting for stop or target — no time-based force exit.",
                }
            )
    return actions
