from __future__ import annotations

from trading_bot.selection import apply_selection_constraints, run_daily_selection
from tests.conftest import FakeProvider, make_candidate, trending_ohlcv


def test_at_most_one_stock_below_50():
    candidates = [
        make_candidate("CHEAP1", 1, 20.0, 90),
        make_candidate("CHEAP2", 2, 15.0, 89),
        make_candidate("BLUE1", 3, 200.0, 80),
        make_candidate("BLUE2", 4, 180.0, 79),
        make_candidate("BLUE3", 5, 170.0, 78),
    ]
    picked = apply_selection_constraints(
        candidates,
        max_picks=4,
        min_top100=3,
        max_below_price=1,
        below_price_threshold=50.0,
    )
    cheap = [p for p in picked if p.last_price < 50]
    assert len(cheap) <= 1
    assert len(picked) <= 4
    assert sum(1 for p in picked if p.stock.in_top100) >= 3


def test_prefills_top100_before_midcaps():
    candidates = [
        make_candidate("MID1", 150, 90.0, 99),
        make_candidate("MID2", 160, 91.0, 98),
        make_candidate("TOP1", 1, 200.0, 70),
        make_candidate("TOP2", 2, 210.0, 69),
        make_candidate("TOP3", 3, 220.0, 68),
        make_candidate("TOP4", 4, 230.0, 67),
    ]
    picked = apply_selection_constraints(
        candidates,
        max_picks=4,
        min_top100=3,
        max_below_price=1,
        below_price_threshold=50.0,
    )
    assert sum(1 for p in picked if p.stock.in_top100) >= 3
    assert len(picked) == 4


def test_manual_tag_is_distinct():
    manual = make_candidate("MANUAL", 8, 300.0, 50, source="manual")
    assert manual.source == "manual"
    assert make_candidate("AI", 1, 300.0, 50).source == "ai_selected"


def test_run_daily_selection_tags_manual_separately(db, settings):
    frame = trending_ohlcv(n=140, seed=9)
    frames = {symbol: frame.copy() for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE", "INFY")}
    provider = FakeProvider(frames)
    picks = run_daily_selection(
        db,
        settings,
        provider,
        as_of="2024-06-28",
        manual_symbol="INFY",
        use_llm=False,
    )
    assert any(p.source == "manual" and p.symbol == "INFY" for p in picks)
    cheap = [p for p in picks if p.source == "ai_selected" and p.last_price < 50]
    assert len(cheap) <= 1
    ai_picks = [p for p in picks if p.source == "ai_selected"]
    assert len(ai_picks) <= 4

