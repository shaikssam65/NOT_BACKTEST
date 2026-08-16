"""Ensemble voting tests."""

from __future__ import annotations

from trading_bot.ensemble import vote_symbol
from trading_bot.indicators import add_indicators, snapshot_from_frame
from trading_bot.strategies import normalize_strategy
from tests.conftest import trending_ohlcv


def test_normalize_ensemble_aliases():
    assert normalize_strategy("ensemble") == "ensemble"
    assert normalize_strategy("voting") == "ensemble"
    assert normalize_strategy("multi_agent") == "ensemble"


def test_vote_symbol_returns_structure(db, settings):
    df = add_indicators(trending_ohlcv(n=120, start_price=200.0))
    row = df.iloc[-1]
    snap = snapshot_from_frame(df)
    vote = vote_symbol(
        "TEST",
        row,
        snap,
        df,
        settings,
        db,
        as_of_date="2024-06-01",
        use_llm=False,
        min_rule_buys=2,
    )
    assert vote.symbol == "TEST"
    assert set(vote.rule_votes) == {
        "sma_crossover",
        "ema_crossover",
        "rsi_pullback",
        "trend_quality",
    }
    assert vote.final_signal in {"buy", "hold", "avoid"}
    assert vote.steps
    assert "FINAL" in vote.steps[-1]
    assert vote.agent_trend["signal"] in {"buy", "hold", "avoid"}
    assert vote.agent_risk["signal"] in {"buy", "hold", "avoid"}


def test_ensemble_buy_requires_agreement(db, settings):
    """With few rule buys, final must not be buy even if agents are optimistic."""
    df = add_indicators(trending_ohlcv(n=80, start_price=50.0, drift=0.0, vol=0.02, seed=99))
    # Flat/noisy series should not get many rule buys
    row = df.iloc[-1]
    snap = snapshot_from_frame(df)
    vote = vote_symbol(
        "FLAT",
        row,
        snap,
        df,
        settings,
        db,
        as_of_date="2024-06-01",
        use_llm=False,
        min_rule_buys=2,
    )
    if vote.rule_buy_count < 2:
        assert vote.final_signal != "buy"
