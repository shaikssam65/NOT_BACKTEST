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
DEFAULT_PICKS = 3
PROFIT_SELL_PCT = 30.0
STOP_PCT = 8.0
TARGET_PCT = 30.0

# UI price bands (₹). User can multi-select any subset.
PRICE_BANDS: list[tuple[str, float, float]] = [
    ("0-50", 0.0, 50.0),
    ("50-100", 50.0, 100.0),
    ("100-200", 100.0, 200.0),
    ("200-500", 200.0, 500.0),
    ("500-1000", 500.0, 1000.0),
    ("1000-5000", 1000.0, 5000.0),
]
DEFAULT_PRICE_BANDS = ["50-100", "100-200", "200-500", "500-1000"]


def _noop(_: str) -> None:
    return None


def price_in_selected_bands(price: float, band_labels: list[str] | None) -> bool:
    """True if price falls in any selected band. Empty/None → all bands allowed."""
    if not band_labels:
        return True
    selected = {str(x) for x in band_labels}
    for label, lo, hi in PRICE_BANDS:
        if label not in selected:
            continue
        # Inclusive lower, exclusive upper except last band includes hi.
        if label == "1000-5000":
            if lo <= price <= hi:
                return True
        elif lo <= price < hi:
            return True
    return False


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
    """Live quote / OHLC from Kite (used as live report when Finnhub has no news)."""
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
        ltp = float(payload.get("last_price") or 0)
        prev = float(ohlc.get("close") or 0)
        day_chg_pct = ((ltp - prev) / prev * 100.0) if prev > 0 and ltp > 0 else 0.0
        high = float(ohlc.get("high") or 0)
        low = float(ohlc.get("low") or 0)
        day_range_pct = ((high - low) / prev * 100.0) if prev > 0 and high > low else 0.0
        return {
            "ltp": ltp,
            "open": float(ohlc.get("open") or 0),
            "high": high,
            "low": low,
            "close": prev,
            "volume": payload.get("volume"),
            "average_price": payload.get("average_price"),
            "oi": payload.get("oi"),
            "change": payload.get("net_change") or payload.get("change"),
            "day_chg_pct": round(day_chg_pct, 3),
            "day_range_pct": round(day_range_pct, 3),
        }
    except Exception:
        logger.debug("Kite quote failed for %s", symbol, exc_info=True)
        return None


def _kite_report_lines(symbol: str, quote: dict[str, Any]) -> list[str]:
    """Human-readable Kite live report used when Finnhub returns no news."""
    lines = [
        f"Kite live {symbol}: LTP {quote.get('ltp')}",
        (
            f"OHLC O {quote.get('open')} H {quote.get('high')} "
            f"L {quote.get('low')} C {quote.get('close')}"
        ),
        f"Day change {quote.get('day_chg_pct')}% · range {quote.get('day_range_pct')}%",
    ]
    if quote.get("volume") is not None:
        lines.append(f"Volume {quote.get('volume')}")
    if quote.get("average_price") is not None:
        lines.append(f"Avg traded price {quote.get('average_price')}")
    return lines


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
    Rank by rule score + Finnhub news (if any) + Kite live quote.
    If Finnhub gives no data for a symbol, fall back fully to Kite quote report.
    """
    log = progress or _noop
    if not candidates:
        return []

    enriched: list[dict[str, Any]] = []
    for c in candidates:
        sym = c["symbol"]
        news_rows = fetch_finnhub_news(sym, settings)
        headlines = [n["headline"] for n in news_rows]
        quote = fetch_kite_quote(sym, settings)
        data_source = "finnhub+kite" if headlines else "kite_fallback"

        live_boost = 0.0
        live_price = float(c["price"])
        sent = 0.0

        if quote and quote.get("ltp"):
            live_price = float(quote["ltp"])
            day_chg = float(quote.get("day_chg_pct") or 0.0)
            # Stronger weight when Finnhub is empty
            cap = 12.0 if not headlines else 8.0
            live_boost = max(-6.0, min(cap, day_chg))
            # Mild preference for trading above prior close when no news
            if not headlines and day_chg > 0:
                live_boost += min(3.0, day_chg * 0.25)

        if headlines:
            sent = _news_sentiment_score(headlines)
            news_weight = 12.0
        else:
            # No Finnhub → synthesise context from Kite only
            if quote:
                headlines = _kite_report_lines(sym, quote)
                news_rows = [{"headline": h, "source": "kite", "url": ""} for h in headlines]
                sent = max(-1.0, min(1.0, live_boost / 10.0))
            news_weight = 0.0  # already in live_boost

        combined = (
            float(c["rule_score"])
            + float(c["rule_buys"]) * 4.0
            + sent * news_weight
            + live_boost * (1.6 if data_source == "kite_fallback" else 1.0)
        )
        note_bits = [
            f"rules {c['rule_buys']}/6 score {c['rule_score']:.0f}",
            f"source={data_source}",
        ]
        if data_source.startswith("finnhub"):
            note_bits.append(f"news_sent {sent:+.2f} ({len([n for n in news_rows if n.get('source') != 'kite'])} articles)")
        if quote and quote.get("ltp"):
            note_bits.append(f"kite LTP {live_price:.2f} day {quote.get('day_chg_pct')}%")

        row = dict(c)
        row["price"] = round(live_price, 2)
        row["news"] = headlines
        row["news_detail"] = news_rows
        row["kite_quote"] = quote
        row["news_sentiment"] = round(sent, 3)
        row["data_source"] = data_source
        row["combined_score"] = round(combined, 2)
        row["pick_note"] = " · ".join(note_bits)
        enriched.append(row)
        log(f"  {sym}: {row['pick_note']}")

    enriched.sort(key=lambda x: x["combined_score"], reverse=True)
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
    place_orders: bool = True,
    price_bands: list[str] | None = None,
) -> dict[str, Any]:
    """
    Research medium established stocks with MA + rule voters + Finnhub news + Kite live quotes,
    then optionally split capital and place BUY orders. No OpenAI.

    place_orders=False → suggest only (show picks + planned qty, no orders).
    price_bands → e.g. ["0-50","100-200"]; empty/None = all bands.
    """
    log = progress or _noop
    as_of = as_of or date.today()
    pick_count = max(2, min(3, int(pick_count)))
    capital = float(capital)
    if capital < 5_000:
        raise ValueError("Capital should be at least ₹5,000")
    bands = list(price_bands) if price_bands else [b[0] for b in PRICE_BANDS]
    mode = "paper" if settings.paper_mode else "live"
    action = "place buys" if place_orders else "suggest only"

    log(
        f"Research ({action}) · capital ₹{capital:,.0f} · picks={pick_count} · "
        f"bands={','.join(bands)} · mode={mode}"
    )
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
        if price <= 0 or not price_in_selected_bands(price, bands):
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
        log("Step 2 — Finnhub news (fallback → Kite live quote if empty)…")
    else:
        log("Step 2 — No Finnhub key → using Kite live quotes only…")

    market_news = fetch_finnhub_market_news(settings)
    if not market_news and settings.kite_ready:
        market_news = [
            {
                "headline": "Finnhub market news empty — ranking uses Kite live quotes + rules",
                "source": "kite_fallback",
                "url": "",
            }
        ]
    picks = rank_with_finnhub_and_kite(
        shortlist, settings, pick_count=pick_count, progress=log
    )
    # Re-check live LTP against bands (quote may differ from last close).
    filtered_picks = []
    for p in picks:
        px = float(p.get("price") or 0)
        if price_in_selected_bands(px, bands):
            filtered_picks.append(p)
        else:
            log(f"  drop {p.get('symbol')} — live ₹{px:.2f} outside selected bands")
    picks = filtered_picks
    log(f"Step 3 — Final picks: {[p['symbol'] for p in picks] or ['none']}")

    if not picks:
        return {
            "ok": True,
            "mode": mode,
            "place_orders": place_orders,
            "price_bands": bands,
            "capital": capital,
            "scanned": scanned,
            "market_news": market_news,
            "shortlist": [
                {k: v for k, v in c.items() if k != "snap"} for c in shortlist[:8]
            ],
            "picks": [],
            "orders": [],
            "note": "No stocks passed rules + price bands + Finnhub/Kite filter today.",
        }

    slice_cap = capital / len(picks)
    orders: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    if place_orders:
        log(f"Step 4 — Split ₹{capital:,.0f} → ₹{slice_cap:,.0f} each · place buys…")
    else:
        log(f"Step 4 — Suggest only · ₹{slice_cap:,.0f} per name (no orders)…")

    for p in picks:
        price = float(p["price"])
        stop = price * (1 - STOP_PCT / 100.0)
        target = price * (1 + TARGET_PCT / 100.0)
        qty = max(0, math.floor(slice_cap / price))
        plan = {
            "symbol": p["symbol"],
            "name": p.get("name"),
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
            "data_source": p.get("data_source"),
        }
        plans.append(plan)
        if not place_orders:
            continue
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

    if place_orders:
        note = (
            "Rules + Finnhub news when available; else Kite live quotes. No ChatGPT. "
            + ("PAPER fills only." if settings.paper_mode else "LIVE orders sent.")
        )
    else:
        note = (
            "Suggestion only — no orders placed. Review picks, then use "
            "“Place buy orders” if you want to buy."
        )

    return {
        "ok": True,
        "mode": mode,
        "place_orders": place_orders,
        "price_bands": bands,
        "capital": capital,
        "slice_capital": round(slice_cap, 2),
        "scanned": scanned,
        "market_news": market_news,
        "picks": plans,
        "orders": orders,
        "rule_voters": [name for name, _ in RULE_COMBO_VOTERS],
        "note": note,
    }
