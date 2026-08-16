"""Simple two-action bot: auto-sell ≥30% Kite profits, research & buy 2–3 medium stocks."""

from __future__ import annotations

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
    """Deprecated stub — use Finnhub helpers."""
    return []


_POS = (
    "profit", "growth", "surge", "rally", "upgrade", "beat", "strong", "record",
    "win", "rise", "gain", "positive", "expansion", "order", "deal", "buy",
)
_NEG = (
    "loss", "fall", "drop", "downgrade", "miss", "weak", "fraud", "probe",
    "ban", "fine", "decline", "cut", "warn", "negative", "slump", "sell",
)


def fetch_finnhub_news(symbol: str, settings: Settings, *, limit: int = 6) -> list[dict[str, Any]]:
    """Company news from Finnhub (NSE symbol tried as SYMBOL.NS)."""
    if not settings.finnhub_ready:
        return []
    token = settings.finnhub_api_key
    assert token
    end = date.today()
    start = end - timedelta(days=14)
    symbols_try = [f"{symbol.upper()}.NS", symbol.upper()]
    out: list[dict[str, Any]] = []
    with httpx.Client(timeout=12.0) as client:
        for sym in symbols_try:
            try:
                r = client.get(
                    "https://finnhub.io/api/v1/company-news",
                    params={
                        "symbol": sym,
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                        "token": token,
                    },
                )
                if r.status_code != 200:
                    continue
                rows = r.json()
                if not isinstance(rows, list) or not rows:
                    continue
                for item in rows[:limit]:
                    headline = str(item.get("headline") or item.get("summary") or "").strip()
                    if not headline:
                        continue
                    out.append(
                        {
                            "headline": headline[:240],
                            "source": item.get("source") or "finnhub",
                            "url": item.get("url") or "",
                            "datetime": item.get("datetime"),
                        }
                    )
                if out:
                    break
            except Exception:
                logger.debug("Finnhub news failed for %s", sym, exc_info=True)
    return out[:limit]


def fetch_finnhub_market_news(settings: Settings, *, limit: int = 8) -> list[dict[str, Any]]:
    """General market news from Finnhub (India/general)."""
    if not settings.finnhub_ready:
        return []
    token = settings.finnhub_api_key
    assert token
    try:
        with httpx.Client(timeout=12.0) as client:
            r = client.get(
                "https://finnhub.io/api/v1/news",
                params={"category": "general", "token": token},
            )
            if r.status_code != 200:
                return []
            rows = r.json()
            if not isinstance(rows, list):
                return []
            out = []
            for item in rows[:limit]:
                headline = str(item.get("headline") or "").strip()
                if headline:
                    out.append(
                        {
                            "headline": headline[:240],
                            "source": item.get("source") or "finnhub",
                            "url": item.get("url") or "",
                        }
                    )
            return out
    except Exception:
        logger.exception("Finnhub market news failed")
        return []


def fetch_kite_quote(symbol: str, settings: Settings) -> dict[str, Any] | None:
    """Live quote / OHLC from Kite (used as live 'report' for the name)."""
    if not settings.kite_ready:
        return None
    try:
        kite = kite_client(settings)
        key = f"NSE:{symbol.upper()}"
        raw = kite.quote([key]) or {}
        payload = raw.get(key) or raw.get(symbol.upper())
        if not isinstance(payload, dict):
            return None
        ohlc = payload.get("ohlc") or {}
        return {
            "ltp": float(payload.get("last_price") or 0),
            "open": float(ohlc.get("open") or 0),
            "high": float(ohlc.get("high") or 0),
            "low": float(ohlc.get("low") or 0),
            "close": float(ohlc.get("close") or 0),
            "volume": payload.get("volume"),
            "oi": payload.get("oi"),
            "change": payload.get("net_change") or payload.get("change"),
        }
    except Exception:
        logger.debug("Kite quote failed for %s", symbol, exc_info=True)
        return None


def _news_sentiment_score(headlines: list[str]) -> float:
    """Simple keyword sentiment in [-1, +1]."""
    if not headlines:
        return 0.0
    pos = neg = 0
    for h in headlines:
        low = h.lower()
        pos += sum(1 for w in _POS if w in low)
        neg += sum(1 for w in _NEG if w in low)
    total = pos + neg
    if total == 0:
        return 0.05 * min(len(headlines), 5)  # slight boost for having coverage
    return (pos - neg) / total


def rank_with_finnhub_and_kite(
    candidates: list[dict[str, Any]],
    settings: Settings,
    *,
    pick_count: int,
    progress: ProgressCb | None = None,
) -> list[dict[str, Any]]:
    """
    Rank by rule score + Finnhub news sentiment + Kite live quote momentum.
    No ChatGPT / OpenAI.
    """
    log = progress or _noop
    if not candidates:
        return []

    enriched: list[dict[str, Any]] = []
    for c in candidates:
        sym = c["symbol"]
        news_rows = fetch_finnhub_news(sym, settings)
        headlines = [n["headline"] for n in news_rows]
        sent = _news_sentiment_score(headlines)
        quote = fetch_kite_quote(sym, settings)
        live_boost = 0.0
        live_price = c["price"]
        if quote and quote.get("ltp"):
            live_price = float(quote["ltp"])
            prev = float(quote.get("close") or 0)
            if prev > 0:
                day_chg = (live_price - prev) / prev * 100.0
                live_boost = max(-5.0, min(8.0, day_chg))  # mild day-move tilt
        combined = (
            float(c["rule_score"])
            + float(c["rule_buys"]) * 4.0
            + sent * 12.0
            + live_boost
        )
        note_bits = [
            f"rules {c['rule_buys']}/6 score {c['rule_score']:.0f}",
            f"news_sent {sent:+.2f} ({len(headlines)} articles)",
        ]
        if quote and quote.get("ltp"):
            note_bits.append(f"kite LTP {live_price:.2f}")
        row = dict(c)
        row["price"] = round(live_price, 2)
        row["news"] = headlines
        row["news_detail"] = news_rows
        row["kite_quote"] = quote
        row["news_sentiment"] = round(sent, 3)
        row["combined_score"] = round(combined, 2)
        row["pick_note"] = " · ".join(note_bits)
        enriched.append(row)
        log(f"  {sym}: {row['pick_note']}")

    enriched.sort(key=lambda x: x["combined_score"], reverse=True)
    # Prefer non-negative news when scores are close
    return enriched[:pick_count]


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
    Research medium established stocks with MA + rule voters + Finnhub news + Kite live quotes,
    then split capital across 2–3 names and place BUY orders. No OpenAI.
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

    if settings.finnhub_ready:
        log("Step 2 — Finnhub company news + Kite live quotes…")
    else:
        log("Step 2 — Finnhub key missing → rules + Kite quotes only (add FINNHUB_API_KEY)…")

    market_news = fetch_finnhub_market_news(settings)
    picks = rank_with_finnhub_and_kite(
        shortlist, settings, pick_count=pick_count, progress=log
    )
    log(f"Step 3 — Final picks: {[p['symbol'] for p in picks] or ['none']}")

    if not picks:
        return {
            "ok": True,
            "mode": mode,
            "capital": capital,
            "scanned": scanned,
            "market_news": market_news,
            "shortlist": [
                {k: v for k, v in c.items() if k != "snap"} for c in shortlist[:8]
            ],
            "picks": [],
            "orders": [],
            "note": "No stocks passed rules + Finnhub/Kite filter today.",
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
            "pick_note": p.get("pick_note"),
            "news": p.get("news") or [],
            "kite_quote": p.get("kite_quote"),
            "news_sentiment": p.get("news_sentiment"),
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
        "market_news": market_news,
        "picks": plans,
        "orders": orders,
        "rule_voters": [name for name, _ in RULE_COMBO_VOTERS],
        "note": (
            "Rules (MA/EMA/RSI/…) + Finnhub news + Kite live quotes. No ChatGPT. "
            + ("PAPER fills only." if settings.paper_mode else "LIVE orders sent.")
        ),
    }
