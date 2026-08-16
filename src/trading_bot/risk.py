"""Hard risk gate — every order must pass validate_order(). No bypasses."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any

from trading_bot.config import RiskConfig, Settings


@dataclass
class OrderIntent:
    symbol: str
    side: str  # BUY / SELL
    entry_price: float
    stop_loss_price: float | None
    target_price: float | None = None
    qty: int | None = None
    source: str = "ai_selected"  # ai_selected | manual
    is_add_on: bool = False  # averaging into a loser


@dataclass
class RiskDecision:
    passed: bool
    reason: str
    qty: int = 0
    risk_amount: float = 0.0
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def position_qty(capital: float, risk_pct: float, entry: float, stop: float) -> int:
    risk_amount = capital * (risk_pct / 100.0)
    per_share = entry - stop
    if per_share <= 0 or risk_amount <= 0 or entry <= 0:
        return 0
    qty = math.floor(risk_amount / per_share)
    max_affordable = math.floor(capital / entry)
    return max(0, min(qty, max_affordable))


def log_risk_event(conn, *, event_type: str, symbol: str | None, decision: RiskDecision) -> None:
    conn.execute(
        """
        INSERT INTO risk_events (created_at, event_type, symbol, passed, reason, details_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _now_iso(),
            event_type,
            symbol,
            1 if decision.passed else 0,
            decision.reason,
            json.dumps(decision.details or {}),
        ),
    )
    conn.commit()


def daily_realized_pnl(conn, day: date | None = None) -> float:
    day = day or date.today()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(pnl), 0) AS pnl
        FROM positions
        WHERE status = 'closed' AND date(exit_time) = ?
        """,
        (day.isoformat(),),
    ).fetchone()
    return float(row["pnl"] if row else 0.0)


def open_position_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE status = 'open'"
    ).fetchone()
    return int(row["n"] if row else 0)


def has_open_position(conn, symbol: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM positions WHERE symbol = ? AND status = 'open' LIMIT 1",
        (symbol.upper(),),
    ).fetchone()
    return row is not None


def is_daily_halted(conn, day: date | None = None) -> bool:
    day = day or date.today()
    row = conn.execute(
        """
        SELECT 1 FROM risk_events
        WHERE event_type = 'daily_halt'
          AND passed = 1
          AND date(created_at) = ?
        LIMIT 1
        """,
        (day.isoformat(),),
    ).fetchone()
    return row is not None


def validate_order(
    intent: OrderIntent,
    settings: Settings,
    conn,
    *,
    available_capital: float | None = None,
    open_positions: int | None = None,
) -> RiskDecision:
    """Hard gate. Manual picks get no special treatment."""
    risk: RiskConfig = settings.risk
    capital = float(available_capital if available_capital is not None else settings.capital)
    symbol = intent.symbol.upper()
    side = intent.side.upper()

    if is_daily_halted(conn):
        decision = RiskDecision(False, "daily_loss_limit_halt_active", details={"symbol": symbol})
        log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
        return decision

    realized = daily_realized_pnl(conn)
    loss_limit = capital * (risk.daily_loss_limit_pct / 100.0)
    if realized < 0 and abs(realized) >= loss_limit:
        decision = RiskDecision(
            False,
            "daily_loss_limit_breached",
            details={"realized_pnl": realized, "limit": loss_limit},
        )
        log_risk_event(conn, event_type="daily_halt", symbol=symbol, decision=RiskDecision(True, "halt"))
        log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
        return decision

    if side == "BUY":
        if intent.stop_loss_price is None or intent.stop_loss_price <= 0:
            decision = RiskDecision(False, "stop_loss_mandatory")
            log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
            return decision
        if intent.entry_price <= 0:
            decision = RiskDecision(False, "invalid_entry_price")
            log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
            return decision
        if intent.stop_loss_price >= intent.entry_price:
            decision = RiskDecision(False, "stop_must_be_below_entry_for_long")
            log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
            return decision

        stop_pct = (intent.entry_price - intent.stop_loss_price) / intent.entry_price * 100.0
        if stop_pct < risk.min_stop_loss_pct or stop_pct > risk.max_stop_loss_pct:
            decision = RiskDecision(
                False,
                "stop_loss_pct_out_of_bounds",
                details={"stop_pct": stop_pct, "min": risk.min_stop_loss_pct, "max": risk.max_stop_loss_pct},
            )
            log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
            return decision

        open_n = open_positions if open_positions is not None else open_position_count(conn)
        if open_n >= risk.max_concurrent_positions:
            decision = RiskDecision(
                False,
                "max_concurrent_positions",
                details={"open": open_n, "max": risk.max_concurrent_positions},
            )
            log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
            return decision

        if has_open_position(conn, symbol):
            if intent.is_add_on or not risk.allow_averaging_down:
                decision = RiskDecision(False, "no_averaging_down_or_duplicate_position")
                log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
                return decision

        qty = intent.qty if intent.qty and intent.qty > 0 else position_qty(
            capital, risk.risk_per_trade_pct, intent.entry_price, intent.stop_loss_price
        )
        max_affordable = math.floor(capital / intent.entry_price) if intent.entry_price > 0 else 0
        if intent.qty and intent.qty > 0:
            qty = min(int(intent.qty), max_affordable)
        if qty <= 0:
            decision = RiskDecision(False, "position_size_zero")
            log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
            return decision

        risk_amount = qty * (intent.entry_price - intent.stop_loss_price)
        decision = RiskDecision(
            True,
            "ok",
            qty=qty,
            risk_amount=round(risk_amount, 2),
            details={"source": intent.source, "stop_pct": round(stop_pct, 3)},
        )
        log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
        return decision

    if side == "SELL":
        # Exits are allowed; sizing comes from the open position.
        decision = RiskDecision(True, "ok_exit", qty=intent.qty or 0)
        log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
        return decision

    decision = RiskDecision(False, f"unsupported_side_{side}")
    log_risk_event(conn, event_type="validate_order", symbol=symbol, decision=decision)
    return decision
