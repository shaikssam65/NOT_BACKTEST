from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from trading_bot.backtest import VALID_STRATEGIES, backtest
from trading_bot.runtime import boot, build_provider, configure_logging
from trading_bot.selection import list_selections, run_daily_selection
from trading_bot.universe import get_universe, refresh_universe

configure_logging()
SETTINGS, CONN = boot()
PROVIDER = build_provider(SETTINGS, CONN)


def _run_scheduled_selection() -> None:
    run_daily_selection(CONN, SETTINGS, PROVIDER, use_llm=SETTINGS.openai_ready)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Kolkata"))
    scheduler.add_job(
        _run_scheduled_selection,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=45),
        id="daily_selection",
        replace_existing=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="NSE Equity Bot — Phase 2",
    description=(
        "Universe, AI decision filter, backtesting, and Kite login. "
        "No live orders. PAPER_MODE is always the default."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8501", "http://localhost:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class BacktestRequest(BaseModel):
    symbol: str
    strategy_name: Literal["rule_based", "ai_filtered", "combined"] = "combined"
    start_date: date
    end_date: date
    capital: float = Field(default=100000, gt=0)
    use_llm: bool = False


class SelectRequest(BaseModel):
    date: date | None = None
    manual_symbol: str | None = None
    use_llm: bool = True


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "phase": 2,
        "paper_mode": SETTINGS.paper_mode,
        "openai_ready": SETTINGS.openai_ready,
        "kite_ready": SETTINGS.kite_ready,
        "universe_size": len(get_universe(CONN)),
    }


@app.get("/universe")
def universe() -> dict:
    stocks = get_universe(CONN)
    return {
        "count": len(stocks),
        "stocks": [
            {
                "symbol": s.symbol,
                "name": s.name,
                "rank": s.market_cap_rank,
                "in_top100": s.in_top100,
                "last_price": s.last_price,
            }
            for s in stocks
        ],
    }


@app.post("/universe/refresh")
def universe_refresh() -> dict:
    stocks = refresh_universe(CONN, SETTINGS)
    return {"count": len(stocks), "source": "nse_or_seed"}


@app.post("/select")
def select(req: SelectRequest) -> dict:
    picks = run_daily_selection(
        CONN,
        SETTINGS,
        PROVIDER,
        as_of=req.date,
        manual_symbol=req.manual_symbol,
        use_llm=req.use_llm,
    )
    return {
        "date": (req.date or date.today()).isoformat(),
        "count": len(picks),
        "picks": [
            {
                "symbol": p.symbol,
                "source": p.source,
                "name": p.stock.name,
                "rank": p.stock.market_cap_rank,
                "in_top100": p.stock.in_top100,
                "last_price": p.last_price,
                "rule_signal": p.indicators.rule_signal,
                "ai_signal": p.ai.signal,
                "combined_signal": p.combined_signal,
                "confidence": p.ai.confidence,
                "stop_loss_pct": p.ai.stop_loss_pct,
                "target_pct": p.ai.target_pct,
                "reasoning": p.ai.reasoning,
            }
            for p in picks
        ],
        "note": "Manual picks are tagged separately and still require the Phase 3 risk gate before any order.",
    }


@app.get("/selections")
def selections(as_of: date | None = Query(default=None, alias="date")) -> dict:
    day = (as_of or date.today()).isoformat()
    return {"date": day, "picks": list_selections(CONN, day)}


@app.post("/backtest")
def run_backtest(req: BacktestRequest) -> dict:
    if req.strategy_name not in VALID_STRATEGIES:
        raise HTTPException(status_code=400, detail="Invalid strategy")
    try:
        result = backtest(
            req.symbol,
            req.strategy_name,
            req.start_date,
            req.end_date,
            req.capital,
            SETTINGS,
            PROVIDER,
            CONN,
            use_llm=req.use_llm,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.to_dict()


@app.get("/ai-decisions")
def ai_decisions(symbol: str | None = None, limit: int = 50) -> dict:
    limit = max(1, min(limit, 200))
    if symbol:
        rows = CONN.execute(
            """
            SELECT created_at, symbol, as_of_date, model, source, signal, confidence,
                   stop_loss_pct, target_pct, reasoning
            FROM ai_decisions
            WHERE symbol = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (symbol.upper(), limit),
        ).fetchall()
    else:
        rows = CONN.execute(
            """
            SELECT created_at, symbol, as_of_date, model, source, signal, confidence,
                   stop_loss_pct, target_pct, reasoning
            FROM ai_decisions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"count": len(rows), "decisions": [dict(r) for r in rows]}


@app.get("/kite/status")
def kite_status() -> dict:
    from trading_bot.kite_auth import kite_redirect_url, profile_status

    settings = SETTINGS
    status = profile_status(settings)
    status["redirect_url"] = kite_redirect_url(settings)
    status["api_key_saved"] = bool(settings.kite_api_key)
    return status
