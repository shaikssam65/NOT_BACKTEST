"""Simple two-action bot: auto-sell ≥30% Kite profits, research & buy 2–3 medium stocks."""

from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from typing import Any, Callable

import httpx

from trading_bot.config import Settings
from trading_bot.data_provider import HistoricalDataProvider
from trading_bot.execution import place_buy
from trading_bot.indicators import add_indicators, snapshot_from_frame
from trading_bot.kite_auth import kite_client
from trading_bot.strategies import RULE_COMBO_VOTERS, rules_combo_vote
from trading_bot.universe import get_universe

logger = logging.getLogger(__name__)
ProgressCb = Callable[[str], None]

# Medium established: skip mega-caps and micro junk.
MIN_RANK = 25
MAX_RANK = 160
# Prefer liquid but not ultra-expensive names for splitting capital.
MAX_PRICE = 800.0
DEFAULT_PICKS = 3
PROFIT_SELL_PCT = 30.0
STOP_PCT = 8.0
TARGET_PCT = 30.0


def _noop(_: str) -> None:
    return None


def fetch_kite_holdings(settings: Settings) -> list[dict[str, Any]]:
    if not settings.kite_ready:
        return []
    try:
        kite = kite_client(settings)
        rows = kite.holdings() or []
    except Exception:
        logger.exception("kite.holdings failed")
        return []
    out: list[dict[str, Any]] = []
    for h in rows:
        qty = int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0)
        if qty <= 0:
            continue
        symbol = str(h.get("tradingsymbol") or "").upper()
        avg = float(h.get("average_price") or 0)
        ltp = float(h.get("last_price") or 0)
        if not symbol or avg <= 0:
            continue
        pnl_pct = ((ltp - avg) / avg) * 100.0 if ltp > 0 else 0.0
        out.append(
            {
                "symbol": symbol,
                "qty": qty,
                "avg_price": round(avg, 2),
                "ltp": round(ltp, 2),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_rs": round((ltp - avg) * qty, 2),
                "exchange": h.get("exchange") or "NSE",
            }
        )
    return out


def auto_sell_profits(
    settings: Settings,
    *,
    min_profit_pct: float = PROFIT_SELL_PCT,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """
    Connect to Kite holdings. If unrealized profit ≥ threshold → place SELL.
    Otherwise leave the position alone. Respects PAPER_MODE.
    """
    log = progress or _noop
    mode = "paper" if settings.paper_mode else "live"
    log(f"Pulling Kite holdings (mode={mode})…")
    holdings = fetch_kite_holdings(settings)
    if not holdings:
        return {
            "ok": True,
            "mode": mode,
            "holdings": [],
            "sold": [],
            "kept": [],
            "note": "No holdings found — connect Kite or account is flat.",
        }

    sold: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for h in holdings:
        if h["pnl_pct"] + 1e-9 < min_profit_pct:
            kept.append({**h, "action": "keep", "reason": f"profit {h['pnl_pct']}% < {min_profit_pct}%"})
            log(f"  KEEP {h['symbol']}: {h['pnl_pct']}%")
            continue

        log(f"  SELL {h['symbol']}: +{h['pnl_pct']}% (≥{min_profit_pct}%)")
        if settings.paper_mode:
            sold.append(
                {
                    **h,
                    "action": "paper_sell",
                    "ok": True,
                    "note": "PAPER — no real order. Turn PAPER_MODE=false for live sell.",
                }
            )
            continue
        try:
            kite = kite_client(settings)
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NSE,
                tradingsymbol=h["symbol"],
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=int(h["qty"]),
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_CNC,
            )
            sold.append({**h, "action": "live_sell", "ok": True, "broker_order_id": str(order_id)})
        except Exception as exc:
            logger.exception("Sell failed for %s", h["symbol"])
            sold.append({**h, "action": "sell_failed", "ok": False, "reason": str(exc)})

    return {
        "ok": True,
        "mode": mode,
        "threshold_pct": min_profit_pct,
        "holdings": holdings,
        "sold": sold,
        "kept": kept,
        "note": (
            f"Checked {len(holdings)} holdings · sold {len(sold)} · kept {len(kept)}."
            + (" PAPER mode — sells are simulated." if settings.paper_mode else "")
        ),
    }


def _fetch_news_headlines(symbol: str, limit: int = 5) -> list[str]:
    """Best-effort headlines for the LLM (Yahoo RSS / yfinance)."""
    headlines: list[str] = []
    # Yahoo Finance RSS
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}.NS&region=US&lang=en-US"
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code == 200 and "<title>" in r.text:
                import re

                titles = re.findall(r"<title>(.*?)</title>", r.text, flags=re.I | re.S)
                for t in titles[1 : limit + 1]:  # skip channel title
                    clean = (
                        t.replace("<![CDATA[", "")
                        .replace("]]>", "")
                        .replace("&amp;", "&")
                        .strip()
                    )
                    if clean and clean not in headlines:
                        headlines.append(clean[:200])
    except Exception:
        logger.debug("RSS news failed for %s", symbol, exc_info=True)

    if headlines:
        return headlines[:limit]

    # yfinance fallback
    try:
        import yfinance as yf

        items = yf.Ticker(f"{symbol}.NS").news or []
        for item in items[:limit]:
            title = (item.get("title") or item.get("content", {}).get("title") or "").strip()
            if title:
                headlines.append(title[:200])
    except Exception:
        logger.debug("yfinance news failed for %s", symbol, exc_info=True)
    return headlines[:limit]


def _llm_rank_with_news(
    candidates: list[dict[str, Any]],
    settings: Settings,
    *,
    pick_count: int,
) -> list[dict[str, Any]]:
    """Ask OpenAI to pick best 2–3 using rule scores + news. Falls back to rule rank."""
    if not candidates:
        return []
    if not settings.openai_ready:
        ranked = sorted(candidates, key=lambda c: c["rule_score"], reverse=True)
        for c in ranked:
            c["llm_note"] = "OpenAI key missing — ranked by rules only"
        return ranked[:pick_count]

    payload = []
    for c in candidates:
        payload.append(
            {
                "symbol": c["symbol"],
                "price": c["price"],
                "rank": c["rank"],
                "rule_score": c["rule_score"],
                "rule_buys": c["rule_buys"],
                "rule_votes": c["rule_votes"],
                "news": c.get("news") or [],
            }
        )
    system = (
        "You are an NSE cash-equity research assistant for medium established stocks. "
        "Pick the best names for a short-horizon swing using ONLY the provided rule scores and news. "
        f"Return JSON: {{\"picks\":[{{\"symbol\":\"X\",\"confidence\":0-100,\"reason\":\"short\"}}]}} "
        f"Return at most {pick_count} picks, highest quality first. Prefer clear uptrend + supportive news. "
        "If news is empty, rely on rules. Never invent prices."
    )
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key, timeout=settings.ai.timeout_seconds)
        resp = client.chat.completions.create(
            model=settings.ai.model,
            temperature=0.2,
            max_tokens=500,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"candidates": payload}, default=str)},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        picks_raw = data.get("picks") or []
        by_sym = {c["symbol"]: c for c in candidates}
        chosen: list[dict[str, Any]] = []
        for p in picks_raw:
            sym = str(p.get("symbol") or "").upper()
            if sym not in by_sym:
                continue
            row = dict(by_sym[sym])
            row["llm_confidence"] = int(p.get("confidence") or 60)
            row["llm_note"] = str(p.get("reason") or "LLM pick")[:300]
            chosen.append(row)
            if len(chosen) >= pick_count:
                break
        if chosen:
            return chosen
    except Exception:
        logger.exception("OpenAI ranking failed — falling back to rules")

    ranked = sorted(candidates, key=lambda c: c["rule_score"], reverse=True)
    for c in ranked:
        c["llm_note"] = "LLM failed — ranked by rules only"
    return ranked[:pick_count]


def research_and_buy(
    conn,
    settings: Settings,
    provider: HistoricalDataProvider,
    *,
    capital: float,
    pick_count: int = DEFAULT_PICKS,
    as_of: date | None = None,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """
    Research medium established stocks with MA + rule voters + OpenAI/news,
    then split capital across 2–3 names and place BUY orders.
    """
    log = progress or _noop
    as_of = as_of or date.today()
    pick_count = max(2, min(3, int(pick_count)))
    capital = float(capital)
    if capital < 5_000:
        raise ValueError("Capital should be at least ₹5,000")
    mode = "paper" if settings.paper_mode else "live"

    log(f"Research & buy · capital ₹{capital:,.0f} · picks={pick_count} · mode={mode}")
    log("Step 1 — Score medium established names (SMA/EMA/RSI/trend/momentum/volume)…")

    universe = get_universe(conn)
    lookback = as_of - timedelta(days=settings.selection.lookback_days + 20)
    scored: list[dict[str, Any]] = []
    scanned = 0

    for stock in universe:
        rank = int(stock.market_cap_rank)
        if rank < MIN_RANK or rank > MAX_RANK:
            continue
        df = provider.get_ohlcv(stock.symbol, lookback, as_of)
        if df.empty or len(df) < settings.indicators.sma_slow + 5:
            continue
        indicated = add_indicators(df, settings.indicators)
        row = indicated.iloc[-1]
        price = float(row["close"])
        if price <= 0 or price > MAX_PRICE:
            continue
        scanned += 1
        score, signal, votes = rules_combo_vote(row, min_buys=2)
        buy_n = sum(1 for v in votes.values() if v == "buy")
        if signal != "buy" and buy_n < 2:
            continue
        # Soft accept high scores even if signal hold
        if signal != "buy" and score < 60:
            continue
        scored.append(
            {
                "symbol": stock.symbol,
                "name": stock.name,
                "rank": rank,
                "price": round(price, 2),
                "rule_score": float(score),
                "rule_signal": signal,
                "rule_buys": buy_n,
                "rule_votes": votes,
                "snap": snapshot_from_frame(indicated),
            }
        )
        if scanned % 40 == 0:
            log(f"  …scanned {scanned}")

    scored.sort(key=lambda c: (c["rule_buys"], c["rule_score"]), reverse=True)
    shortlist = scored[:12]
    log(f"  Rule shortlist: {len(shortlist)} / scanned {scanned}")

    log("Step 2 — Fetch news + OpenAI pick…")
    for c in shortlist:
        c["news"] = _fetch_news_headlines(c["symbol"])
        log(f"  {c['symbol']}: rules={c['rule_buys']}/6 score={c['rule_score']:.0f} news={len(c['news'])}")

    picks = _llm_rank_with_news(shortlist, settings, pick_count=pick_count)
    log(f"Step 3 — Final picks: {[p['symbol'] for p in picks] or ['none']}")

    if not picks:
        return {
            "ok": True,
            "mode": mode,
            "capital": capital,
            "scanned": scanned,
            "shortlist": [
                {k: v for k, v in c.items() if k != "snap"} for c in shortlist[:8]
            ],
            "picks": [],
            "orders": [],
            "note": "No stocks passed rules + LLM filter today.",
        }

    slice_cap = capital / len(picks)
    orders: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    log(f"Step 4 — Split ₹{capital:,.0f} → ₹{slice_cap:,.0f} each · place buys…")

    for p in picks:
        price = float(p["price"])
        stop = price * (1 - STOP_PCT / 100.0)
        target = price * (1 + TARGET_PCT / 100.0)
        qty = max(0, math.floor(slice_cap / price))
        plan = {
            "symbol": p["symbol"],
            "price": price,
            "qty": qty,
            "slice_capital": round(slice_cap, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "rule_buys": p.get("rule_buys"),
            "llm_note": p.get("llm_note"),
            "news": p.get("news") or [],
        }
        plans.append(plan)
        if qty <= 0:
            orders.append({"symbol": p["symbol"], "ok": False, "reason": "qty_zero"})
            continue
        result = place_buy(
            conn,
            settings,
            symbol=p["symbol"],
            entry_price=price,
            stop_loss=stop,
            target=target,
            strategy="simple_bot",
            source="ai_selected",
            available_capital=slice_cap,
            qty=qty,
        )
        orders.append({"symbol": p["symbol"], **plan, **result})
        if result.get("ok"):
            log(f"  BUY {p['symbol']} × {result.get('qty')} @ {price}")
        else:
            log(f"  REJECTED {p['symbol']}: {result.get('reason')}")

    return {
        "ok": True,
        "mode": mode,
        "capital": capital,
        "slice_capital": round(slice_cap, 2),
        "scanned": scanned,
        "picks": plans,
        "orders": orders,
        "rule_voters": [name for name, _ in RULE_COMBO_VOTERS],
        "note": (
            f"Bought with rules (MA/EMA/RSI/trend/…) + OpenAI/news. "
            + ("PAPER fills only." if settings.paper_mode else "LIVE orders sent.")
        ),
    }
