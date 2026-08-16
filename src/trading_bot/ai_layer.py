from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from trading_bot.config import Settings
from trading_bot.data_provider import last_n_bars
from trading_bot.models import AISignal, IndicatorSnapshot, Signal

logger = logging.getLogger(__name__)

PROMPT_VERSION = "ai-filter-v1"

SYSTEM_PROMPT = """You are a conservative NSE cash-equity decision FILTER, not a standalone oracle.
You never recompute indicators from prices — use the pre-computed values provided.
Output JSON only with this exact shape:
{"signal":"buy"|"hold"|"avoid","confidence":0-100,"stop_loss_pct":float,"target_pct":float,"reasoning":"short text"}
Rules:
- Long-only cash equities (no F&O, no shorting).
- Prefer avoid when the trend is mixed or indicators conflict.
- Only buy when the provided rule_signal is buy AND price action supports a trend-following entry.
- stop_loss_pct typically 1.5-3.0; target_pct typically 2-2.5x the stop; never set target below stop.
- reasoning must be one or two short sentences, no markdown.
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clamp_signal_fields(
    signal: str,
    confidence: int | float,
    stop_loss_pct: float,
    target_pct: float,
    reasoning: str,
    *,
    min_stop: float = 0.5,
    max_stop: float = 8.0,
) -> tuple[Signal, int, float, float, str]:
    normalized = str(signal).strip().lower()
    if normalized not in {"buy", "hold", "avoid"}:
        normalized = "hold"
    conf = int(max(0, min(100, round(float(confidence)))))
    stop = float(stop_loss_pct)
    target = float(target_pct)
    stop = max(min_stop, min(max_stop, stop))
    if target <= stop:
        target = round(stop * 2.0, 2)
    target = max(stop + 0.25, min(20.0, target))
    text = " ".join(str(reasoning).split())[:400] or "No reasoning provided."
    return normalized, conf, round(stop, 2), round(target, 2), text  # type: ignore[return-value]


def heuristic_ai_signal(indicators: IndicatorSnapshot, settings: Settings) -> AISignal:
    """Deterministic stand-in when OpenAI is unavailable. Stricter than the rule layer."""
    score = indicators.rule_score
    rsi = indicators.rsi
    signal: Signal
    if (
        indicators.rule_signal == "buy"
        and indicators.sma_trend == "bullish"
        and indicators.ema_trend == "bullish"
        and rsi is not None
        and 45 <= rsi <= 62
        and score >= 78
    ):
        signal = "buy"
        confidence = min(90, 50 + (score - 70))
        reasoning = (
            "Heuristic filter agrees with the uptrend: SMA and EMA aligned, "
            f"RSI {rsi:.1f} is not overbought, rule score {score}."
        )
    elif indicators.rule_signal == "avoid" or (rsi is not None and rsi > 74):
        signal = "avoid"
        confidence = min(90, 40 + (50 - min(score, 50)))
        reasoning = (
            f"Heuristic filter avoids: rule signal {indicators.rule_signal}, "
            f"score {score}, RSI {rsi}."
        )
    else:
        signal = "hold"
        confidence = 55
        reasoning = (
            f"Heuristic filter is mixed: rule signal {indicators.rule_signal}, "
            f"score {score}."
        )

    atr_pct = None
    if indicators.atr and indicators.last_close:
        atr_pct = (indicators.atr / indicators.last_close) * 100
    stop = settings.backtest.default_stop_loss_pct
    if atr_pct:
        stop = max(stop, min(settings.risk.max_stop_loss_pct, atr_pct * settings.backtest.atr_stop_mult))
    target = max(settings.backtest.default_target_pct, stop * 2.0)
    signal, confidence, stop, target, reasoning = clamp_signal_fields(
        signal,
        confidence,
        stop,
        target,
        reasoning,
        min_stop=settings.risk.min_stop_loss_pct,
        max_stop=settings.risk.max_stop_loss_pct,
    )
    return AISignal(
        signal=signal,
        confidence=confidence,
        stop_loss_pct=stop,
        target_pct=target,
        reasoning=reasoning,
        source="heuristic_fallback",
        raw_response=None,
    )


def _build_input_payload(
    symbol: str,
    indicators: IndicatorSnapshot,
    ohlcv,
    as_of_date: str | None,
) -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "symbol": symbol,
        "as_of_date": as_of_date,
        "indicators": indicators.to_dict(),
        "recent_bars": last_n_bars(ohlcv, 10),
        "bar_count": int(getattr(ohlcv, "__len__", lambda: 0)()),
    }


def _parse_llm_json(text: str, settings: Settings) -> AISignal:
    data = json.loads(text)
    signal, confidence, stop, target, reasoning = clamp_signal_fields(
        data.get("signal", "hold"),
        data.get("confidence", 50),
        float(data.get("stop_loss_pct", settings.backtest.default_stop_loss_pct)),
        float(data.get("target_pct", settings.backtest.default_target_pct)),
        data.get("reasoning", ""),
        min_stop=settings.risk.min_stop_loss_pct,
        max_stop=settings.risk.max_stop_loss_pct,
    )
    return AISignal(
        signal=signal,
        confidence=confidence,
        stop_loss_pct=stop,
        target_pct=target,
        reasoning=reasoning,
        source="openai",
        raw_response=text,
    )


def _call_openai(payload: dict[str, Any], settings: Settings) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key, timeout=settings.ai.timeout_seconds)
    response = client.chat.completions.create(
        model=settings.ai.model,
        temperature=settings.ai.temperature,
        max_tokens=settings.ai.max_tokens,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, default=str),
            },
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned empty content")
    return content


def _log_decision(
    conn,
    *,
    symbol: str,
    as_of_date: str | None,
    payload: dict[str, Any],
    result: AISignal,
    model: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ai_decisions (
            created_at, symbol, as_of_date, model, source, input_json,
            raw_response, signal, confidence, stop_loss_pct, target_pct, reasoning
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now_iso(),
            symbol.upper(),
            as_of_date,
            model,
            result.source,
            json.dumps(payload, default=str),
            result.raw_response,
            result.signal,
            result.confidence,
            result.stop_loss_pct,
            result.target_pct,
            result.reasoning,
        ),
    )
    conn.commit()


def get_ai_signal(
    symbol: str,
    historical_data,
    indicators: IndicatorSnapshot,
    settings: Settings,
    conn,
    *,
    as_of_date: str | None = None,
    use_llm: bool = True,
) -> AISignal:
    """LLM filter on top of pre-computed indicators. Always writes an audit row."""
    payload = _build_input_payload(symbol, indicators, historical_data, as_of_date)
    result: AISignal | None = None
    if use_llm and settings.openai_ready:
        try:
            raw = _call_openai(payload, settings)
            result = _parse_llm_json(raw, settings)
        except Exception:
            logger.exception("OpenAI call failed for %s; using heuristic fallback", symbol)
    if result is None:
        result = heuristic_ai_signal(indicators, settings)
        result.raw_response = json.dumps({"fallback": True, "reason": "llm_unavailable_or_disabled"})
    model_name = settings.ai.model if result.source == "openai" else "heuristic_fallback"
    _log_decision(
        conn,
        symbol=symbol,
        as_of_date=as_of_date,
        payload=payload,
        result=result,
        model=model_name,
    )
    return result


def combine_signals(rule_signal: Signal, ai_signal: Signal) -> Signal:
    """Act only when the rule-based trend and the AI filter both say buy."""
    if rule_signal == "buy" and ai_signal == "buy":
        return "buy"
    if rule_signal == "avoid" or ai_signal == "avoid":
        return "avoid"
    return "hold"


def combined_score(rule_score: int, ai: AISignal, combined: Signal) -> float:
    if combined != "buy":
        ai_part = ai.confidence * 0.25 if ai.signal != "avoid" else 0.0
        return round(rule_score * 0.4 + ai_part, 2)
    return round(rule_score * 0.5 + ai.confidence * 0.5, 2)
