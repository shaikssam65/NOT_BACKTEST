from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import httpx

from trading_bot.config import ROOT, Settings
from trading_bot.models import UniverseStock

logger = logging.getLogger(__name__)

NSE_NIFTY100 = "https://archives.nseindia.com/content/indices/ind_nifty100list.csv"
NSE_NIFTY200 = "https://archives.nseindia.com/content/indices/ind_nifty200list.csv"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv,application/xhtml+xml",
    "Referer": "https://www.nseindia.com/",
}

# Yahoo Finance tickers that differ from the NSE symbol.
YAHOO_OVERRIDES = {
    "LTM": "LTIM",
    "TMCV": "TATAMOTORS",
    "TMPV": "TATAMOTORS",
    "UNITDSPR": "UNITDSPR",
    "ETERNAL": "ETERNAL",
    "M&M": "M&M",
    "M&MFIN": "M&MFIN",
    "BAJAJ-AUTO": "BAJAJ-AUTO",
    "GVT&D": "GVT&D",
}


def yahoo_ticker(symbol: str) -> str:
    mapped = YAHOO_OVERRIDES.get(symbol.upper(), symbol.upper())
    return f"{mapped}.NS"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_seed_stocks() -> list[UniverseStock]:
    csv_text = resources.files("trading_bot.data").joinpath("nse_top200.csv").read_text(
        encoding="utf-8"
    )
    reader = csv.DictReader(io.StringIO(csv_text))
    stocks: list[UniverseStock] = []
    for row in reader:
        symbol = row["symbol"].strip().upper()
        stocks.append(
            UniverseStock(
                symbol=symbol,
                name=row["name"].strip(),
                market_cap_rank=int(row["rank"]),
                last_price=float(row["last_price"]) if row.get("last_price") else None,
                yahoo_ticker=yahoo_ticker(symbol),
            )
        )
    return stocks


def persist_universe(conn, stocks: list[UniverseStock], refreshed_at: str | None = None) -> None:
    stamp = refreshed_at or _now_iso()
    conn.execute("DELETE FROM universe")
    conn.executemany(
        """
        INSERT INTO universe (symbol, name, market_cap_rank, last_price, yahoo_ticker, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (s.symbol, s.name, s.market_cap_rank, s.last_price, s.yahoo_ticker, stamp)
            for s in stocks
        ],
    )
    conn.execute(
        "INSERT INTO universe_meta(key, value) VALUES('refreshed_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (stamp,),
    )
    conn.commit()


def get_universe(conn) -> list[UniverseStock]:
    rows = conn.execute(
        "SELECT symbol, name, market_cap_rank, last_price, yahoo_ticker "
        "FROM universe ORDER BY market_cap_rank"
    ).fetchall()
    if not rows:
        stocks = load_seed_stocks()
        persist_universe(conn, stocks)
        return stocks
    return [
        UniverseStock(
            symbol=row["symbol"],
            name=row["name"],
            market_cap_rank=row["market_cap_rank"],
            last_price=row["last_price"],
            yahoo_ticker=row["yahoo_ticker"],
        )
        for row in rows
    ]


def get_stock(conn, symbol: str) -> UniverseStock | None:
    row = conn.execute(
        "SELECT symbol, name, market_cap_rank, last_price, yahoo_ticker "
        "FROM universe WHERE symbol = ?",
        (symbol.upper(),),
    ).fetchone()
    if not row:
        return None
    return UniverseStock(
        symbol=row["symbol"],
        name=row["name"],
        market_cap_rank=row["market_cap_rank"],
        last_price=row["last_price"],
        yahoo_ticker=row["yahoo_ticker"],
    )


def _parse_nse_csv(text: str) -> list[str]:
    reader = csv.DictReader(io.StringIO(text))
    symbols: list[str] = []
    for row in reader:
        symbol = (row.get("Symbol") or row.get("SYMBOL") or "").strip().upper()
        if symbol:
            symbols.append(symbol)
    return symbols


def _fetch_csv(url: str) -> list[str]:
    with httpx.Client(headers=NSE_HEADERS, timeout=20.0, follow_redirects=True) as client:
        # Warm the NSE cookie jar, then fetch the CSV.
        try:
            client.get("https://www.nseindia.com")
        except httpx.HTTPError:
            pass
        response = client.get(url)
        response.raise_for_status()
        return _parse_nse_csv(response.text)


def refresh_universe(conn, settings: Settings | None = None) -> list[UniverseStock]:
    """Refresh ranks from NSE index CSVs; fall back to the bundled seed."""
    del settings  # reserved for authenticated NSE/Kite refresh later
    seed_by_symbol = {s.symbol: s for s in load_seed_stocks()}
    try:
        nifty100 = set(_fetch_csv(NSE_NIFTY100))
        nifty200 = _fetch_csv(NSE_NIFTY200)
        if len(nifty200) < 150:
            raise RuntimeError(f"unexpected Nifty 200 size: {len(nifty200)}")
        stocks: list[UniverseStock] = []
        for rank, symbol in enumerate(nifty200, start=1):
            seed = seed_by_symbol.get(symbol)
            in_top100 = symbol in nifty100
            # If NSE order disagrees with Nifty 100 membership, keep membership
            # as the source of truth for the "at least 3 from Top 100" rule.
            effective_rank = rank
            if in_top100 and rank > 100:
                effective_rank = 100
            if not in_top100 and rank <= 100:
                effective_rank = 101
            stocks.append(
                UniverseStock(
                    symbol=symbol,
                    name=seed.name if seed else symbol,
                    market_cap_rank=effective_rank,
                    last_price=seed.last_price if seed else None,
                    yahoo_ticker=yahoo_ticker(symbol),
                )
            )
        # Restore a stable rank 1..N after membership adjustments.
        stocks.sort(key=lambda s: (s.market_cap_rank, s.symbol))
        for i, stock in enumerate(stocks, start=1):
            stock.market_cap_rank = i if stock.market_cap_rank <= 100 else max(i, 101)
        persist_universe(conn, stocks)
        logger.info("Refreshed universe from NSE: %s names", len(stocks))
        return stocks
    except Exception:
        logger.exception("NSE universe refresh failed; using bundled seed")
        stocks = load_seed_stocks()
        persist_universe(conn, stocks)
        return stocks


def export_universe_csv(stocks: list[UniverseStock], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "symbol", "name", "last_price"])
        for stock in stocks:
            writer.writerow(
                [stock.market_cap_rank, stock.symbol, stock.name, stock.last_price or ""]
            )


def universe_age_days(conn) -> float | None:
    row = conn.execute(
        "SELECT value FROM universe_meta WHERE key = 'refreshed_at'"
    ).fetchone()
    if not row:
        return None
    try:
        stamp = datetime.fromisoformat(row["value"])
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() / 86400
    except ValueError:
        return None
