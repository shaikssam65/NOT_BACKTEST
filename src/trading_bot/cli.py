from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from trading_bot.backtest import backtest
from trading_bot.strategies import VALID_STRATEGIES
from trading_bot.auto_trade import run_daily_auto_trade
from trading_bot.runtime import boot, build_provider, configure_logging
from trading_bot.selection import run_daily_selection
from trading_bot.universe import get_universe, refresh_universe


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(
        prog="trading-bot",
        description="Phase 2: NSE universe, AI filter, backtesting, and Kite dashboard. No live orders.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create SQLite tables and seed the universe")
    sub.add_parser("refresh-universe", help="Refresh NSE Top 200 (falls back to bundled seed)")

    select_p = sub.add_parser("select", help="Run the daily 3-4 stock selection")
    select_p.add_argument("--date", dest="as_of", default=None, help="YYYY-MM-DD (default: today)")
    select_p.add_argument("--manual", dest="manual_symbol", default=None, help="Optional extra symbol")
    select_p.add_argument(
        "--no-llm",
        action="store_true",
        help="Use the heuristic AI fallback instead of OpenAI",
    )

    bt = sub.add_parser("backtest", help="Backtest one symbol / strategy")
    bt.add_argument("--symbol", required=True)
    bt.add_argument("--strategy", default="rules_combo", choices=list(VALID_STRATEGIES))
    bt.add_argument("--start", required=True, help="YYYY-MM-DD")
    bt.add_argument("--end", required=True, help="YYYY-MM-DD")
    bt.add_argument("--capital", type=float, default=None)
    bt.add_argument(
        "--use-llm",
        action="store_true",
        help="Call OpenAI on each candidate bar (slow/expensive). Default is heuristic AI.",
    )

    auto = sub.add_parser("auto-trade", help="Daily select + paper/live orders via risk gate")
    auto.add_argument("--strategy", default="rules_combo", choices=list(VALID_STRATEGIES))
    auto.add_argument("--date", dest="as_of", default=None)
    auto.add_argument("--capital", type=float, default=None, help="₹ capital for position sizing")
    auto.add_argument("--no-llm", action="store_true")

    sub.add_parser(
        "take-profits",
        help="Auto-sell bot (+ optional Kite) positions at ≥30% profit — for 10:00 IST daily job",
    )

    serve = sub.add_parser("serve", help="Start the FastAPI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    dash = sub.add_parser("dashboard", help="Open the Streamlit dashboard (Kite login + backtests)")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8501)

    args = parser.parse_args(argv)
    settings, conn = boot()
    provider = build_provider(settings, conn)

    if args.command == "init-db":
        stocks = get_universe(conn)
        _print(
            {
                "database": str(settings.database_path),
                "universe_size": len(stocks),
                "paper_mode": settings.paper_mode,
            }
        )
        return 0

    if args.command == "refresh-universe":
        stocks = refresh_universe(conn, settings)
        _print({"count": len(stocks), "top5": [s.symbol for s in stocks[:5]]})
        return 0

    if args.command == "select":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        picks = run_daily_selection(
            conn,
            settings,
            provider,
            as_of=as_of,
            manual_symbol=args.manual_symbol,
            use_llm=not args.no_llm,
        )
        _print(
            {
                "date": as_of.isoformat(),
                "paper_mode": settings.paper_mode,
                "picks": [
                    {
                        "symbol": p.symbol,
                        "source": p.source,
                        "rank": p.stock.market_cap_rank,
                        "last_price": p.last_price,
                        "rule": p.indicators.rule_signal,
                        "ai": p.ai.signal,
                        "combined": p.combined_signal,
                        "confidence": p.ai.confidence,
                        "stop_loss_pct": p.ai.stop_loss_pct,
                        "target_pct": p.ai.target_pct,
                        "reasoning": p.ai.reasoning,
                    }
                    for p in picks
                ],
            }
        )
        return 0

    if args.command == "backtest":
        capital = args.capital if args.capital is not None else settings.capital
        result = backtest(
            args.symbol,
            args.strategy,
            args.start,
            args.end,
            capital,
            settings,
            provider,
            conn,
            use_llm=args.use_llm,
        )
        payload = result.to_dict()
        payload.pop("equity_curve", None)
        payload.pop("trades", None)
        payload["equity_points"] = len(result.equity_curve)
        payload["sample_trades"] = result.trades[:5]
        _print(payload)
        return 0

    if args.command == "auto-trade":
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        result = run_daily_auto_trade(
            conn,
            settings,
            provider,
            strategy=args.strategy,
            as_of=as_of,
            use_llm=not args.no_llm,
            trade_capital=args.capital,
        )
        _print(result)
        return 0

    if args.command == "take-profits":
        from trading_bot.simple_bot import auto_sell_profits

        _print(auto_sell_profits(settings))
        return 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run("trading_bot.api:app", host=args.host, port=args.port, reload=False)
        return 0

    if args.command == "dashboard":
        from pathlib import Path

        from streamlit.web import cli as stcli

        app = str(Path(__file__).resolve().with_name("dashboard.py"))
        sys.argv = [
            "streamlit",
            "run",
            app,
            "--server.address",
            args.host,
            "--server.port",
            str(args.port),
            "--browser.gatherUsageStats",
            "false",
        ]
        stcli.main()
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
