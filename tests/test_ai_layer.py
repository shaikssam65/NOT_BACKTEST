from __future__ import annotations

from trading_bot.ai_layer import combine_signals, get_ai_signal, heuristic_ai_signal
from trading_bot.indicators import add_indicators, snapshot_from_frame
from tests.conftest import make_snapshot, trending_ohlcv


def test_combine_requires_both_buy():
    assert combine_signals("buy", "buy") == "buy"
    assert combine_signals("buy", "hold") == "hold"
    assert combine_signals("hold", "buy") == "hold"
    assert combine_signals("buy", "avoid") == "avoid"
    assert combine_signals("avoid", "buy") == "avoid"


def test_heuristic_agrees_on_healthy_uptrend(settings):
    snap = make_snapshot(rule_signal="buy", rule_score=80, rsi=55)
    result = heuristic_ai_signal(snap, settings)
    assert result.signal == "buy"
    assert 0 <= result.confidence <= 100
    assert result.target_pct > result.stop_loss_pct
    assert result.source == "heuristic_fallback"


def test_heuristic_avoids_overbought(settings):
    snap = make_snapshot(rule_signal="avoid", rule_score=20, rsi=80)
    result = heuristic_ai_signal(snap, settings)
    assert result.signal == "avoid"


def test_get_ai_signal_logs_even_without_llm(db, settings):
    df = add_indicators(trending_ohlcv())
    snap = snapshot_from_frame(df)
    result = get_ai_signal(
        "AAA",
        df,
        snap,
        settings,
        db,
        as_of_date="2024-06-01",
        use_llm=False,
    )
    row = db.execute("SELECT COUNT(*) AS n FROM ai_decisions WHERE symbol='AAA'").fetchone()
    assert row["n"] == 1
    logged = db.execute("SELECT source, signal FROM ai_decisions WHERE symbol='AAA'").fetchone()
    assert logged["source"] == "heuristic_fallback"
    assert result.signal in {"buy", "hold", "avoid"}
