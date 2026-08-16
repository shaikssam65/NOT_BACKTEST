"""Decision engines: rules_combo (6 voters) and dual_agents (2 AI agents)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

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
from trading_bot.strategies import RULE_COMBO_VOTERS, rules_combo_vote

Mode = Literal["rules_combo", "dual_agents", "combined"]

AGENT_TREND = """You are Agent-Trend: a conservative NSE cash-equity DAILY trader (short-horizon swing, not long-term investing).
Use only the pre-computed indicators. JSON only:
{"signal":"buy"|"hold"|"avoid","confidence":0-100,"stop_loss_pct":float,"target_pct":float,"reasoning":"short text"}
Buy only on clear uptrend alignment suitable for a short-horizon move (SMA+EMA bullish, RSI not overbought).
Prefer stop_loss_pct around 1.0-2.5 and target_pct around 2.0-4.0. Prefer hold when unsure.
"""

AGENT_RISK = """You are Agent-Risk: a skeptical NSE cash-equity DAILY RISK controller (short-horizon, not buy-and-hold).
Use only the pre-computed indicators. JSON only:
{"signal":"buy"|"hold"|"avoid","confidence":0-100,"stop_loss_pct":float,"target_pct":float,"reasoning":"short text"}
Your job is to VETO weak setups. Prefer avoid/hold unless risk/reward and volume support a buy.
stop_loss_pct typically 1.0-2.5; target at least 1.5x stop and usually under 5%.
"""


@dataclass
class DecisionVote:
    symbol: str
    mode: Mode
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


def _empty_agent() -> dict[str, Any]:
    return {
        "signal": "hold",
        "confidence": 0,
        "stop_loss_pct": 1.5,
        "target_pct": 3.0,
        "reasoning": "not used in rules_combo",
        "source": "n/a",
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
            import json

            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key, timeout=settings.ai.timeout_seconds)
            response = client.chat.completions.create(
                model=settings.ai.model,
                temperature=settings.ai.temperature,
                max_tokens=settings.ai.max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, default=str)},
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


def decide_rules_combo(
    symbol: str,
    row: pd.Series,
    indicators: IndicatorSnapshot,
    settings: Settings,
    *,
    min_rule_buys: int = 3,
) -> DecisionVote:
    """Mode 1: 6 rule voters only — no AI agents."""
    steps: list[str] = []
    score, signal, votes = rules_combo_vote(row, min_buys=min_rule_buys)
    buy_count = sum(1 for s in votes.values() if s == "buy")
    for name, fn in RULE_COMBO_VOTERS:
        sc, sig = fn(row)  # type: ignore[operator]
        steps.append(f"Rule {name}: {sig} (score {sc})")
    steps.append(f"Rule tally: {buy_count}/6 buys · avg score {score}")
    stop = settings.auto_trade.daily_stop_loss_pct
    target = settings.auto_trade.daily_target_pct
    if signal == "buy":
        reasoning = f"Rules combo BUY: {buy_count}/6 rule voters agree (need ≥{min_rule_buys})."
        final_score = float(score)
    elif signal == "avoid":
        reasoning = f"Rules combo AVOID: too many avoids ({buy_count}/6 buys)."
        final_score = float(score) * 0.3
    else:
        reasoning = f"Rules combo HOLD: only {buy_count}/6 buys (need ≥{min_rule_buys})."
        final_score = float(score) * 0.4
    steps.append(f"FINAL: {signal} | stop {stop}% | target {target}%")
    return DecisionVote(
        symbol=symbol.upper(),
        mode="rules_combo",
        last_price=float(indicators.last_close),
        rule_votes=votes,
        rule_buy_count=buy_count,
        rule_score_avg=float(score),
        agent_trend=_empty_agent(),
        agent_risk=_empty_agent(),
        final_signal=signal,
        final_score=round(final_score, 2),
        stop_loss_pct=stop,
        target_pct=target,
        reasoning=reasoning,
        steps=steps,
    )


def decide_dual_agents(
    symbol: str,
    indicators: IndicatorSnapshot,
    historical_data,
    settings: Settings,
    conn,
    *,
    as_of_date: str | None = None,
    use_llm: bool = True,
) -> DecisionVote:
    """Mode 2: Agent-Trend + Agent-Risk must both buy — no rule voting."""
    steps: list[str] = ["Mode: dual_agents (rules not used for the decision)"]
    # Soft rule context for heuristics only
    snap = indicators
    agent_trend = _call_agent(
        agent_name="agent_trend",
        system_prompt=AGENT_TREND,
        symbol=symbol,
        historical_data=historical_data,
        indicators=snap,
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
        indicators=snap,
        settings=settings,
        conn=conn,
        as_of_date=as_of_date,
        use_llm=use_llm,
    )
    steps.append(f"Agent-Trend: {agent_trend.signal} ({agent_trend.confidence})")
    steps.append(f"Agent-Risk: {agent_risk.signal} ({agent_risk.confidence})")

    if agent_trend.signal == "buy" and agent_risk.signal == "buy":
        final: Signal = "buy"
        reasoning = (
            f"Dual agents BUY. Trend: {agent_trend.reasoning} Risk: {agent_risk.reasoning}"
        )
    elif agent_trend.signal == "avoid" or agent_risk.signal == "avoid":
        final = "avoid"
        reasoning = "Dual agents AVOID: at least one agent vetoed."
    else:
        final = "hold"
        reasoning = (
            f"Dual agents HOLD: need both buy "
            f"(trend={agent_trend.signal}, risk={agent_risk.signal})."
        )

    stop = max(agent_trend.stop_loss_pct, agent_risk.stop_loss_pct)
    target = max(agent_trend.target_pct, agent_risk.target_pct, stop * 1.5)
    conf = (agent_trend.confidence + agent_risk.confidence) / 2.0
    final_score = round(conf + (10 if final == "buy" else 0), 2)
    if final != "buy":
        final_score = round(final_score * 0.4, 2)
    steps.append(f"FINAL: {final} | stop {stop}% | target {target}%")
    return DecisionVote(
        symbol=symbol.upper(),
        mode="dual_agents",
        last_price=float(indicators.last_close),
        rule_votes={},
        rule_buy_count=0,
        rule_score_avg=0.0,
        agent_trend=_ai_to_dict(agent_trend),
        agent_risk=_ai_to_dict(agent_risk),
        final_signal=final,
        final_score=final_score,
        stop_loss_pct=stop,
        target_pct=target,
        reasoning=reasoning,
        steps=steps,
    )


def decide_combined(
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
) -> DecisionVote:
    """Mode 3: enough rule buys AND both AI agents buy."""
    steps: list[str] = ["Mode: combined (rules + dual agents)"]
    score, _sig, votes = rules_combo_vote(row, min_buys=min_rule_buys)
    buy_count = sum(1 for s in votes.values() if s == "buy")
    avoid_count = sum(1 for s in votes.values() if s == "avoid")
    for name, fn in RULE_COMBO_VOTERS:
        sc, sig = fn(row)  # type: ignore[operator]
        steps.append(f"Rule {name}: {sig} (score {sc})")
    steps.append(f"Rule tally: {buy_count}/6 buys")

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

    if avoid_count >= 4:
        final: Signal = "avoid"
        reasoning = "Combined AVOID: majority of rules avoid."
    elif (
        buy_count >= min_rule_buys
        and agent_trend.signal == "buy"
        and agent_risk.signal == "buy"
    ):
        final = "buy"
        reasoning = (
            f"Combined BUY: {buy_count}/6 rules + both agents. "
            f"Trend: {agent_trend.reasoning} Risk: {agent_risk.reasoning}"
        )
    elif agent_trend.signal == "avoid" or agent_risk.signal == "avoid":
        final = "avoid"
        reasoning = "Combined AVOID: agent veto."
    else:
        final = "hold"
        reasoning = (
            f"Combined HOLD: need ≥{min_rule_buys} rule buys and both agents "
            f"(got {buy_count}; trend={agent_trend.signal}, risk={agent_risk.signal})."
        )

    stop = max(agent_trend.stop_loss_pct, agent_risk.stop_loss_pct)
    target = max(agent_trend.target_pct, agent_risk.target_pct, stop * 1.5)
    conf = (agent_trend.confidence + agent_risk.confidence) / 2.0
    final_score = round(score * 0.4 + conf * 0.6 + buy_count * 3, 2)
    if final != "buy":
        final_score = round(final_score * 0.4, 2)
    steps.append(f"FINAL: {final} | stop {stop}% | target {target}%")
    return DecisionVote(
        symbol=symbol.upper(),
        mode="combined",
        last_price=float(indicators.last_close),
        rule_votes=votes,
        rule_buy_count=buy_count,
        rule_score_avg=float(score),
        agent_trend=_ai_to_dict(agent_trend),
        agent_risk=_ai_to_dict(agent_risk),
        final_signal=final,
        final_score=final_score,
        stop_loss_pct=stop,
        target_pct=target,
        reasoning=reasoning,
        steps=steps,
    )


def decide_symbol(
    mode: Mode,
    symbol: str,
    row: pd.Series,
    indicators: IndicatorSnapshot,
    historical_data,
    settings: Settings,
    conn,
    *,
    as_of_date: str | None = None,
    use_llm: bool = True,
) -> DecisionVote:
    if mode == "rules_combo":
        return decide_rules_combo(symbol, row, indicators, settings)
    if mode == "combined":
        return decide_combined(
            symbol,
            row,
            indicators,
            historical_data,
            settings,
            conn,
            as_of_date=as_of_date,
            use_llm=use_llm,
        )
    return decide_dual_agents(
        symbol,
        indicators,
        historical_data,
        settings,
        conn,
        as_of_date=as_of_date,
        use_llm=use_llm,
    )


# Back-compat for older tests / callers
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
    min_rule_buys: int = 3,
    mode: Mode = "rules_combo",
) -> DecisionVote:
    if mode == "rules_combo":
        return decide_rules_combo(symbol, row, indicators, settings, min_rule_buys=min_rule_buys)
    if mode == "combined":
        return decide_combined(
            symbol,
            row,
            indicators,
            historical_data,
            settings,
            conn,
            as_of_date=as_of_date,
            use_llm=use_llm,
            min_rule_buys=min_rule_buys,
        )
    return decide_dual_agents(
        symbol,
        indicators,
        historical_data,
        settings,
        conn,
        as_of_date=as_of_date,
        use_llm=use_llm,
    )
