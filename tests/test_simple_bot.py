"""Simple bot tests."""

from __future__ import annotations

from datetime import date

from trading_bot.simple_bot import (
    affordable_pick_count,
    manual_buy,
    manual_sell_holdings,
    place_selected_buys,
    plan_buys_from_capital,
    price_in_selected_bands,
    research_and_buy,
    shares_for_capital,
)
from tests.conftest import FakeProvider, trending_ohlcv


def test_manual_sell_empty_selection(settings):
    result = manual_sell_holdings(settings, [])
    assert result["ok"] is False
    assert result["sold"] == []


def test_manual_sell_missing_holding(settings):
    result = manual_sell_holdings(settings, [{"symbol": "NOSUCH", "qty": 1}])
    assert result["ok"] is True
    assert result["sold"][0]["ok"] is False
    assert result["sold"][0]["reason"] == "not_in_kite_holdings"


def test_manual_buy_requires_symbol(db, settings):
    result = manual_buy(db, settings, symbol="")
    assert result["ok"] is False
    assert result["reason"] == "symbol_required"


def test_manual_buy_with_price(db, settings):
    result = manual_buy(
        db,
        settings,
        symbol="INFY",
        qty=2,
        entry_price=100.0,
    )
    assert result["ok"] is True
    assert result["qty"] == 2
    assert result["price"] == 100.0
    assert result["order"]["ok"] is True


def test_price_bands():
    assert price_in_selected_bands(75.0, ["50-100"]) is True
    assert price_in_selected_bands(75.0, ["0-50"]) is False
    assert price_in_selected_bands(120.0, ["100-200", "500-1000"]) is True
    assert price_in_selected_bands(2500.0, ["1000-5000"]) is True
    assert price_in_selected_bands(99.0, None) is True


def test_qty_from_capital_not_random():
    assert shares_for_capital(100.0, 10_000.0) == 100
    assert shares_for_capital(350.0, 1_000.0) == 2
    assert shares_for_capital(2000.0, 1_000.0) == 0
    picks = [
        {"symbol": "AAA", "price": 100.0},
        {"symbol": "BBB", "price": 200.0},
        {"symbol": "CCC", "price": 5000.0},
    ]
    assert affordable_pick_count(picks, 10_000.0, 3) == 2
    plans = plan_buys_from_capital(picks[:2], 10_000.0)
    assert plans[0]["qty"] == 50
    assert plans[1]["qty"] == 25


def test_place_selected_empty(db, settings):
    result = place_selected_buys(db, settings, [], [], capital=10_000.0)
    assert result["ok"] is False


def test_research_and_buy_runs(db, settings):
    frames = {
        "AAA": trending_ohlcv(n=100, start_price=120.0, seed=1),
        "BBB": trending_ohlcv(n=100, start_price=90.0, seed=2),
        "CCC": trending_ohlcv(n=100, start_price=150.0, seed=3),
        "DDD": trending_ohlcv(n=100, start_price=40.0, seed=4),
        "EEE": trending_ohlcv(n=100, start_price=80.0, seed=5),
    }
    # conftest ranks: AAA=1 filtered out (MIN_RANK=25), EEE=150 ok
    provider = FakeProvider(frames)
    result = research_and_buy(
        db,
        settings,
        provider,
        capital=60_000.0,
        pick_count=2,
        as_of=date(2024, 5, 15),
        place_orders=False,
        price_bands=["50-100", "100-200"],
    )
    assert result["ok"] is True
    assert result["capital"] == 60_000.0
    assert result["place_orders"] is False
    assert result["orders"] == []
    assert "orders" in result
