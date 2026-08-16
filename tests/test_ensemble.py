"""Strategy decision tests — rules_combo + dual_agents only."""

from __future__ import annotations

from trading_bot.ensemble import decide_dual_agents, decide_rules_combo, vote_symbol
from trading_bot.indicators import add_indicators, snapshot_from_frame
from trading_bot.strategies import PRIMARY_STRATEGIES, normalize_strategy, rules_combo_vote
from tests.conftest import trending_ohlcv


def test_only_two_primary_strategies():
    assert "rules_combo" in PRIMARY_STRATEGIES
    assert "dual_agents" in PRIMARY_STRATEGIES
    assert "small_swing" in PRIMARY_STRATEGIES
    assert len(PRIMARY_STRATEGIES) == 3


def test_normalize_aliases_to_two_modes():
    assert normalize_strategy("rules_combo") == "rules_combo"
    assert normalize_strategy("ensemble") == "rules_combo"
    assert normalize_strategy("combined") == "dual_agents"
    assert normalize_strategy("dual_agents") == "dual_agents"
    assert normalize_strategy("agents") == "dual_agents"


def test_rules_combo_has_six_voters(db, settings):
    df = add_indicators(trending_ohlcv(n=120, start_price=200.0))
    row = df.iloc[-1]
    score, signal, votes = rules_combo_vote(row, min_buys=3)
    assert len(votes) == 6
    assert signal in {"buy", "hold", "avoid"}
    snap = snapshot_from_frame(df)
    vote = decide_rules_combo("TEST", row, snap, settings, min_rule_buys=3)
    assert vote.mode == "rules_combo"
    assert vote.agent_trend["source"] == "n/a"
    assert "FINAL" in vote.steps[-1]


def test_dual_agents_structure(db, settings):
    df = add_indicators(trending_ohlcv(n=120, start_price=200.0))
    snap = snapshot_from_frame(df)
    vote = decide_dual_agents(
        "TEST",
        snap,
        df,
        settings,
        db,
        as_of_date="2024-06-01",
        use_llm=False,
    )
    assert vote.mode == "dual_agents"
    assert vote.rule_votes == {}
    assert vote.final_signal in {"buy", "hold", "avoid"}
    assert vote.agent_trend["signal"] in {"buy", "hold", "avoid"}
    assert vote.agent_risk["signal"] in {"buy", "hold", "avoid"}


def test_vote_symbol_compat(db, settings):
    df = add_indicators(trending_ohlcv(n=100, start_price=150.0))
    row = df.iloc[-1]
    snap = snapshot_from_frame(df)
    vote = vote_symbol(
        "AAA",
        row,
        snap,
        df,
        settings,
        db,
        as_of_date="2024-06-01",
        use_llm=False,
        mode="rules_combo",
    )
    assert vote.final_signal in {"buy", "hold", "avoid"}
