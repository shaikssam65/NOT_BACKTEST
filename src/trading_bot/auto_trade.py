"""Daily automated trading loop — PAPER_MODE by default.

Default path uses ensemble voting (4 rule strategies + 2 AI agents).
Returns a detailed activity log so the UI can show what happened.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

from trading_bot.ai_layer import combined_score, get_ai_signal, heuristic_ai_signal
from trading_bot.config import Settings
from trading_bot.data_provider import HistoricalDataProvider
from trading_bot.ensemble import vote_symbol
from trading_bot.execution import list_open_positions, manage_open_positions, place_buy
from trading_bot.indicators import add_indicators, snapshot_from_frame
from trading_bot.models import AISignal, Candidate
from trading_bot.selection import apply_selection_constraints, persist_selections
from trading_bot.strategies import final_signal, needs_ai, normalize_strategy, rule_signal_for
from trading_bot.universe import get_universe

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str], None]


def _noop_progress(_: str) -> None:
    return None


def _stop_target_pct(stop_pct: float, target_pct: float, last: float, settings: Settings) -> tuple[float, float]:
    # Prefer day-trade style bounds from auto_trade config.
    stop_pct = float(stop_pct or settings.auto_trade.daily_stop_loss_pct)
    target_pct = float(target_pct or settings.auto_trade.daily_target_pct)
    stop_pct = max(settings.risk.min_stop_loss_pct, min(settings.risk.max_stop_loss_pct, stop_pct))
    # Cap target for daily style (avoid multi-week style 8–10% targets).
    target_pct = max(stop_pct * 1.5, min(5.0, target_pct))
    stop = last * (1 - stop_pct / 100.0)
    target = last * (1 + target_pct / 100.0)
    return stop, target


def _exit_plan(entry: float, stop: float, target: float, qty: int) -> dict[str, Any]:
    risk = (entry - stop) * qty
    reward = (target - entry) * qty
    return {
        "entry": round(entry, 2),
        "stop_loss": round(stop, 2),
        "target": round(target, 2),
        "qty": qty,
        "risk_rs": round(risk, 2),
        "reward_rs": round(reward, 2),
        "rr": round(reward / risk, 2) if risk > 0 else None,
        "style": "daily",
        "exit_when": (
            f"Daily-trade plan: SELL if LTP ≤ {stop:.2f} (stop) or LTP ≥ {target:.2f} (target). "
            "No forced exit by calendar day — only stop or target closes the trade."
        ),
    }


def run_daily_auto_trade(
    conn,
    settings: Settings,
    provider: HistoricalDataProvider,
    *,
    strategy: str = "ensemble",
    as_of: date | None = None,
    use_llm: bool = True,
    manage_exits: bool = True,
    trade_capital: float | None = None,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """
    1) Manage open positions (SL/target vs live LTP when Kite connected)
    2) Scan universe — ensemble votes OR single strategy
    3) Place paper (or live if PAPER_MODE=false) orders through the risk gate
    """
    report = progress or _noop_progress
    strategy = normalize_strategy(strategy)
    as_of = as_of or date.today()
    mode = "paper" if settings.paper_mode else "live"
    capital = float(trade_capital if trade_capital is not None else settings.capital)
    if capital <= 0:
        raise ValueError("trade_capital must be > 0")

    activity: list[str] = []

    def log(msg: str) -> None:
        activity.append(msg)
        report(msg)
        logger.info(msg)

    log(f"Start auto-trade · date={as_of.isoformat()} · mode={mode} · strategy={strategy}")
    log(
        f"Style: DAILY setups (short-horizon stops/targets) · "
        f"exits only on SL/target · capital ₹{capital:,.0f}"
    )

    exit_actions: list[dict[str, Any]] = []
    if manage_exits:
        log("Step 1/4 — Checking open positions vs stop/target only (no time force-exit)…")
        fallback: dict[str, float] = {}
        for pos in list_open_positions(conn):
            sym = pos["symbol"]
            try:
                hist = provider.get_ohlcv(sym, as_of - timedelta(days=10), as_of)
                if not hist.empty:
                    fallback[sym] = float(hist.iloc[-1]["close"])
            except Exception:
                pass
        exit_actions = manage_open_positions(
            conn,
            settings,
            as_of=as_of,
            fallback_prices=fallback or None,
        )
        if not exit_actions:
            log("  No open positions to manage.")
        else:
            for a in exit_actions:
                log(f"  Exit check {a.get('symbol')}: {a.get('action')} ({a.get('reason', a.get('ltp', ''))})")
    else:
        log("Step 1/4 — Skip exit management.")

    universe = get_universe(conn)
    lookback_start = as_of - timedelta(days=settings.selection.lookback_days + 20)
    log(f"Step 2/4 — Scanning {len(universe)} stocks…")

    votes: list[dict[str, Any]] = []
    detected: list[dict[str, Any]] = []
    scored: list[Candidate] = []
    scanned = 0
    skipped_data = 0

    for stock in universe:
        df = provider.get_ohlcv(stock.symbol, lookback_start, as_of)
        if df.empty or len(df) < settings.indicators.sma_slow + 5:
            skipped_data += 1
            continue
        indicated = add_indicators(df, settings.indicators)
        row = indicated.iloc[-1]
        snap = snapshot_from_frame(indicated)
        scanned += 1

        if strategy == "ensemble":
            vote = vote_symbol(
                stock.symbol,
                row,
                snap,
                indicated,
                settings,
                conn,
                as_of_date=as_of.isoformat(),
                use_llm=bool(use_llm and settings.openai_ready),
                min_rule_buys=2,
            )
            vote_dict = vote.to_dict()
            votes.append(vote_dict)
            if vote.final_signal == "buy":
                detected.append(
                    {
                        "symbol": vote.symbol,
                        "price": vote.last_price,
                        "rule_buys": vote.rule_buy_count,
                        "sma": vote.rule_votes.get("sma_crossover"),
                        "ema": vote.rule_votes.get("ema_crossover"),
                        "rsi": vote.rule_votes.get("rsi_pullback"),
                        "trend": vote.rule_votes.get("trend_quality"),
                        "agent_trend": vote.agent_trend.get("signal"),
                        "agent_risk": vote.agent_risk.get("signal"),
                        "score": vote.final_score,
                        "stop_pct": vote.stop_loss_pct,
                        "target_pct": vote.target_pct,
                        "reasoning": vote.reasoning,
                    }
                )
                ai = AISignal(
                    signal=vote.final_signal,
                    confidence=int(round((vote.agent_trend["confidence"] + vote.agent_risk["confidence"]) / 2)),
                    stop_loss_pct=vote.stop_loss_pct,
                    target_pct=vote.target_pct,
                    reasoning=vote.reasoning,
                    source="ensemble",
                )
                snap.rule_score = int(round(vote.rule_score_avg))
                snap.rule_signal = "buy" if vote.rule_buy_count >= 2 else "hold"
                scored.append(
                    Candidate(
                        stock=stock,
                        indicators=snap,
                        ai=ai,
                        combined_signal="buy",
                        combined_score=vote.final_score,
                        source="ai_selected",
                    )
                )
                log(
                    f"  DETECTED BUY {vote.symbol} @ {vote.last_price:.2f} "
                    f"({vote.rule_buy_count}/4 rules, agents agree) score={vote.final_score}"
                )
            continue

        # Non-ensemble single-strategy path
        score, rule_sig = rule_signal_for(strategy, row)
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
        cand = Candidate(
            stock=stock,
            indicators=snap,
            ai=ai,
            combined_signal=combined,
            combined_score=combined_score(score, ai, combined),
            source="ai_selected",
        )
        scored.append(cand)
        if combined == "buy":
            detected.append(
                {
                    "symbol": cand.symbol,
                    "price": cand.last_price,
                    "rule": rule_sig,
                    "ai": ai.signal,
                    "score": cand.combined_score,
                    "stop_pct": ai.stop_loss_pct,
                    "target_pct": ai.target_pct,
                    "reasoning": ai.reasoning,
                }
            )
            log(f"  DETECTED BUY {cand.symbol} @ {cand.last_price:.2f} score={cand.combined_score}")

    log(f"  Scanned {scanned} stocks ({skipped_data} skipped — insufficient history).")
    log(f"  Buy candidates before constraints: {len(detected)}")

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
    log(f"Step 3/4 — After selection constraints: {len(picks)} pick(s) → {[p.symbol for p in picks] or ['none']}")

    log(f"Step 4/4 — Placing orders with capital ₹{capital:,.0f}…")
    order_results: list[dict[str, Any]] = []
    entry_plans: list[dict[str, Any]] = []
    for pick in picks:
        last = pick.last_price
        stop, target = _stop_target_pct(
            pick.ai.stop_loss_pct, pick.ai.target_pct, last, settings
        )
        result = place_buy(
            conn,
            settings,
            symbol=pick.symbol,
            entry_price=last,
            stop_loss=stop,
            target=target,
            strategy=strategy,
            source=pick.source,
            available_capital=capital,
        )
        row_out = {
            "symbol": pick.symbol,
            "entry_price": round(last, 2),
            "stop_loss": round(stop, 2),
            "target": round(target, 2),
            "stop_pct": pick.ai.stop_loss_pct,
            "target_pct": pick.ai.target_pct,
            "reasoning": pick.ai.reasoning,
            **result,
        }
        order_results.append(row_out)
        if result.get("ok"):
            plan = _exit_plan(
                last,
                stop,
                target,
                int(result.get("qty") or 0),
            )
            plan["symbol"] = pick.symbol
            plan["mode"] = result.get("mode")
            entry_plans.append(plan)
            log(
                f"  ORDER {pick.symbol}: BUY {result.get('qty')} @ {last:.2f} "
                f"| SL {stop:.2f} | Target {target:.2f} | {result.get('mode')}"
            )
        else:
            log(f"  ORDER REJECTED {pick.symbol}: {result.get('reason')}")

    if not picks:
        log("  No buys today — ensemble/rules did not agree strongly enough (this is normal).")

    opens = list_open_positions(conn)
    log(f"Done. Open positions now: {len(opens)}")

    # Keep vote table manageable for UI (buys first, then top scores)
    votes_sorted = sorted(
        votes,
        key=lambda v: (v.get("final_signal") == "buy", float(v.get("final_score") or 0)),
        reverse=True,
    )
    votes_ui = [v for v in votes_sorted if v.get("final_signal") == "buy"]
    votes_ui += [v for v in votes_sorted if v.get("final_signal") != "buy"][:30]

    return {
        "date": as_of.isoformat(),
        "mode": mode,
        "strategy": strategy,
        "paper_mode": settings.paper_mode,
        "trade_capital": capital,
        "style": "daily",
        "intended_hold_days": settings.auto_trade.intended_hold_days,
        "scanned": scanned,
        "skipped_data": skipped_data,
        "detected": detected,
        "picks": [p.symbol for p in picks],
        "orders": order_results,
        "entry_plans": entry_plans,
        "exits": exit_actions,
        "votes": votes_ui,
        "activity": activity,
        "open_positions": opens,
        "note": (
            "DAILY setups · PAPER · exit only on stop or target (never force-flat by day). "
            "Run each market day for new entries. Nothing guarantees profit."
            if settings.paper_mode
            else "DAILY setups · LIVE · exit only on stop/target. SEBI algo registration required."
        ),
    }
