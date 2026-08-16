"""Ensemble voting: multiple rule strategies + dual AI agents."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from trading_bot.ai_layer import (
    clamp_signal_fields,
    heuristic_ai_signal,
    _build_input_payload,
    _log_decision,
    _parse_llm_json,
)
from trading_bot.config import Settings
from trading_bot.models import AISignal, IndicatorSnapshot, Signal
from trading_bot.strategies import (
    signal_ema_crossover,
    signal_rsi_pullback,
    signal_sma_crossover,
    signal_trend_quality,
)

RULE_VOTERS = (
    ("sma_crossover", signal_sma_crossover),
    ("ema_crossover", signal_ema_crossover),
    ("rsi_pullback", signal_rsi_pullback),
    ("trend_quality", signal_trend_quality),
)

AGENT_TREND = """You are Agent-Trend: a conservative NSE cash-equity DAILY trader (short-horizon swing, not long-term investing).
Use only the pre-computed indicators. JSON only:
{"signal":"buy"|"hold"|"avoid","confidence":0-100,"stop_loss_pct":float,"target_pct":float,"reasoning":"short text"}
Buy only on clear uptrend alignment suitable for a same-session / next-few-sessions move (SMA+EMA bullish, RSI not overbought).
Prefer stop_loss_pct around 1.0-2.0 and target_pct around 2.0-3.5 (daily-trade scale). Prefer hold when unsure.
"""

AGENT_RISK = """You are Agent-Risk: a skeptical NSE cash-equity DAILY RISK controller (short-horizon, not buy-and-hold).
Use only the pre-computed indicators. JSON only:
{"signal":"buy"|"hold"|"avoid","confidence":0-100,"stop_loss_pct":float,"target_pct":float,"reasoning":"short text"}
Your job is to VETO weak setups for day-style trades. Prefer avoid/hold unless risk/reward and volume support a buy that can work within a short horizon.
stop_loss_pct typically 1.0-2.5; target at least 1.5x stop and usually under 5%. Never suggest holding for weeks.
"""


@dataclass
class EnsembleVote:
    symbol: str
    last_price: float
    rule_votes: dict[str, str]
    rule_buy_count: int
    rule_score_avg: float
    agent_trend: dict[str, Any]
    agent_risk: dict[str, Any]
    final_signal: Signal
    final_score: float
    stop_loss_pct: float
    target_pct: float
    reasoning: str
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ai_to_dict(ai: AISignal) -> dict[str, Any]:
    return {
        "signal": ai.signal,
        "confidence": ai.confidence,
        "stop_loss_pct": ai.stop_loss_pct,
        "target_pct": ai.target_pct,
        "reasoning": ai.reasoning,
        "source": ai.source,
    }


def _call_agent(
    *,
    agent_name: str,
    system_prompt: str,
    symbol: str,
    historical_data,
    indicators: IndicatorSnapshot,
    settings: Settings,
    conn,
    as_of_date: str | None,
    use_llm: bool,
) -> AISignal:
    payload = _build_input_payload(symbol, indicators, historical_data, as_of_date)
    payload["agent"] = agent_name
    result: AISignal | None = None
    if use_llm and settings.openai_ready:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key, timeout=settings.ai.timeout_seconds)
            response = client.chat.completions.create(
                model=settings.ai.model,
                temperature=settings.ai.temperature,
                max_tokens=settings.ai.max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": __import__("json").dumps(payload, default=str)},
                ],
            )
            content = response.choices[0].message.content
            if content:
                result = _parse_llm_json(content, settings)
                result.source = f"openai:{agent_name}"
        except Exception:
            result = None
    if result is None:
        result = heuristic_ai_signal(indicators, settings)
        # Risk agent is stricter in fallback mode
        if agent_name == "agent_risk" and result.signal == "buy" and result.confidence < 70:
            signal, conf, stop, target, reasoning = clamp_signal_fields(
                "hold",
                55,
                result.stop_loss_pct,
                result.target_pct,
                f"Agent-Risk heuristic veto: confidence {result.confidence} too low.",
                min_stop=settings.risk.min_stop_loss_pct,
                max_stop=settings.risk.max_stop_loss_pct,
            )
            result = AISignal(signal, conf, stop, target, reasoning, "heuristic:agent_risk")
        else:
            result.source = f"heuristic:{agent_name}"
            result.reasoning = f"[{agent_name}] {result.reasoning}"
    _log_decision(
        conn,
        symbol=symbol,
        as_of_date=as_of_date,
        payload=payload,
        result=result,
        model=f"{settings.ai.model}:{agent_name}" if "openai" in result.source else result.source,
    )
    return result


def vote_symbol(
    symbol: str,
    row: pd.Series,
    indicators: IndicatorSnapshot,
    historical_data,
    settings: Settings,
    conn,
    *,
    as_of_date: str | None = None,
    use_llm: bool = True,
    min_rule_buys: int = 2,
) -> EnsembleVote:
    """Vote across 4 rule strategies + Agent-Trend + Agent-Risk."""
    steps: list[str] = []
    rule_votes: dict[str, str] = {}
    scores: list[int] = []
    for name, fn in RULE_VOTERS:
        score, signal = fn(row)
        rule_votes[name] = signal
        scores.append(score)
        steps.append(f"Rule {name}: {signal} (score {score})")

    buy_count = sum(1 for s in rule_votes.values() if s == "buy")
    avoid_count = sum(1 for s in rule_votes.values() if s == "avoid")
    avg_score = sum(scores) / max(len(scores), 1)
    steps.append(f"Rule tally: {buy_count} buy / {avoid_count} avoid / avg score {avg_score:.1f}")

    # Pre-filter: only spend AI calls when rules have some support
    agent_trend = heuristic_ai_signal(indicators, settings)
    agent_risk = heuristic_ai_signal(indicators, settings)
    if buy_count >= 1:
        agent_trend = _call_agent(
            agent_name="agent_trend",
            system_prompt=AGENT_TREND,
            symbol=symbol,
            historical_data=historical_data,
            indicators=indicators,
            settings=settings,
            conn=conn,
            as_of_date=as_of_date,
            use_llm=use_llm,
        )
        agent_risk = _call_agent(
            agent_name="agent_risk",
            system_prompt=AGENT_RISK,
            symbol=symbol,
            historical_data=historical_data,
            indicators=indicators,
            settings=settings,
            conn=conn,
            as_of_date=as_of_date,
            use_llm=use_llm,
        )
    steps.append(f"Agent-Trend: {agent_trend.signal} ({agent_trend.confidence})")
    steps.append(f"Agent-Risk: {agent_risk.signal} ({agent_risk.confidence})")

    # Final decision: need enough rule buys AND both agents buy
    final: Signal = "hold"
    if avoid_count >= 3:
        final = "avoid"
        reasoning = "Majority of rule voters avoid."
    elif (
        buy_count >= min_rule_buys
        and agent_trend.signal == "buy"
        and agent_risk.signal == "buy"
    ):
        final = "buy"
        reasoning = (
            f"Ensemble BUY: {buy_count}/4 rules + both AI agents agree. "
            f"Trend: {agent_trend.reasoning} Risk: {agent_risk.reasoning}"
        )
    elif agent_trend.signal == "avoid" or agent_risk.signal == "avoid" or avoid_count >= 2:
        final = "avoid"
        reasoning = "Ensemble AVOID: risk veto or multiple rule avoids."
    else:
        final = "hold"
        reasoning = (
            f"Ensemble HOLD: need ≥{min_rule_buys} rule buys and both agents buy "
            f"(got {buy_count} rule buys; trend={agent_trend.signal}, risk={agent_risk.signal})."
        )

    stop = max(agent_trend.stop_loss_pct, agent_risk.stop_loss_pct)
    target = max(agent_trend.target_pct, agent_risk.target_pct, stop * 2.0)
    conf = (agent_trend.confidence + agent_risk.confidence) / 2.0
    final_score = round(avg_score * 0.45 + conf * 0.55 + buy_count * 5, 2)
    if final != "buy":
        final_score = round(final_score * 0.4, 2)

    steps.append(f"FINAL: {final} | stop {stop}% | target {target}%")
    return EnsembleVote(
        symbol=symbol.upper(),
        last_price=float(indicators.last_close),
        rule_votes=rule_votes,
        rule_buy_count=buy_count,
        rule_score_avg=round(avg_score, 2),
        agent_trend=_ai_to_dict(agent_trend),
        agent_risk=_ai_to_dict(agent_risk),
        final_signal=final,
        final_score=final_score,
        stop_loss_pct=stop,
        target_pct=target,
        reasoning=reasoning,
        steps=steps,
    )
