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

NSE_INDEX_BASE = "https://archives.nseindia.com/content/indices/"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv,application/xhtml+xml",
    "Referer": "https://www.nseindia.com/",
}

# Broad-market + strategy NSE indices used as research filters (union ≈ Total Market).
NSE_INDEX_FILES: list[tuple[str, str]] = [
    ("Nifty 50", "ind_nifty50list.csv"),
    ("Nifty Next 50", "ind_niftynext50list.csv"),
    ("Nifty 100", "ind_nifty100list.csv"),
    ("Nifty 200", "ind_nifty200list.csv"),
    ("Nifty Midcap 50", "ind_niftymidcap50list.csv"),
    ("Nifty Midcap 100", "ind_niftymidcap100list.csv"),
    ("Nifty Midcap 150", "ind_niftymidcap150list.csv"),
    ("Nifty Smallcap 50", "ind_niftysmallcap50list.csv"),
    ("Nifty Smallcap 100", "ind_niftysmallcap100list.csv"),
    ("Nifty Smallcap 250", "ind_niftysmallcap250list.csv"),
    ("Nifty 500", "ind_nifty500list.csv"),
    ("Nifty MidSmallcap 400", "ind_niftymidsmallcap400list.csv"),
    ("Nifty Microcap 250", "ind_niftymicrocap250_list.csv"),
    ("Nifty Smallcap250 Quality 50", "ind_niftySmallcap250_Quality50_list.csv"),
    ("Nifty Smallcap250 Momentum Quality 100", "ind_niftySmallcap250MomentumQuality100_list.csv"),
    ("Nifty Total Market", "ind_niftytotalmarket_list.csv"),
]
INDEX_LABELS: list[str] = [label for label, _ in NSE_INDEX_FILES]
# Default: all indexes selected (unique pool ≈ Nifty Total Market ~750).
DEFAULT_INDEX_FILTERS: list[str] = list(INDEX_LABELS)
UNIVERSE_LABEL = "NSE Nifty indices (union ≈ Total Market ~750)"
PRIMARY_INDEX = "Nifty Total Market"

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


def _parse_indices(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for p in str(raw).replace(",", "|").split("|") if p.strip()]
    return parts


def _join_indices(indices: list[str] | set[str] | None) -> str:
    if not indices:
        return ""
    return "|".join(sorted({str(x) for x in indices if x}))


def load_seed_stocks() -> list[UniverseStock]:
    """Bundled Total Market seed with index membership tags."""
    data = resources.files("trading_bot.data")
    csv_text = None
    for name in ("nse_universe.csv", "nse_nifty500.csv", "nse_top200.csv"):
        try:
            csv_text = data.joinpath(name).read_text(encoding="utf-8")
            break
        except (FileNotFoundError, OSError):
            continue
    if not csv_text:
        raise FileNotFoundError("No bundled universe CSV found")
    reader = csv.DictReader(io.StringIO(csv_text))
    stocks: list[UniverseStock] = []
    for row in reader:
        symbol = row["symbol"].strip().upper()
        indices = _parse_indices(row.get("indices"))
        if not indices:
            indices = [PRIMARY_INDEX]
        stocks.append(
            UniverseStock(
                symbol=symbol,
                name=row["name"].strip(),
                market_cap_rank=int(row["rank"]),
                last_price=float(row["last_price"]) if row.get("last_price") else None,
                yahoo_ticker=yahoo_ticker(symbol),
                indices=indices,
            )
        )
    return stocks


def _ensure_indices_column(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(universe)").fetchall()}
    if "indices" not in cols:
        conn.execute("ALTER TABLE universe ADD COLUMN indices TEXT")
        conn.commit()


def persist_universe(conn, stocks: list[UniverseStock], refreshed_at: str | None = None) -> None:
    _ensure_indices_column(conn)
    stamp = refreshed_at or _now_iso()
    conn.execute("DELETE FROM universe")
    conn.executemany(
        """
        INSERT INTO universe (symbol, name, market_cap_rank, last_price, yahoo_ticker, updated_at, indices)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                s.symbol,
                s.name,
                s.market_cap_rank,
                s.last_price,
                s.yahoo_ticker,
                stamp,
                _join_indices(s.indices),
            )
            for s in stocks
        ],
    )
    conn.execute(
        "INSERT INTO universe_meta(key, value) VALUES('refreshed_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (stamp,),
    )
    conn.commit()


def get_universe(
    conn,
    *,
    index_filters: list[str] | None = None,
) -> list[UniverseStock]:
    """
    Return universe stocks, optionally filtered by NSE index membership.
    index_filters empty/None → all stocks in DB.
    """
    _ensure_indices_column(conn)
    rows = conn.execute(
        "SELECT symbol, name, market_cap_rank, last_price, yahoo_ticker, indices "
        "FROM universe ORDER BY market_cap_rank"
    ).fetchall()
    if not rows:
        stocks = load_seed_stocks()
        persist_universe(conn, stocks)
        stocks_out = stocks
    else:
        stocks_out = [
            UniverseStock(
                symbol=row["symbol"],
                name=row["name"],
                market_cap_rank=row["market_cap_rank"],
                last_price=row["last_price"],
                yahoo_ticker=row["yahoo_ticker"],
                indices=_parse_indices(row["indices"] if "indices" in row.keys() else None),
            )
            for row in rows
        ]

    if not index_filters:
        return stocks_out
    wanted = {str(x) for x in index_filters}
    return [s for s in stocks_out if wanted.intersection(s.indices or [])]


def get_stock(conn, symbol: str) -> UniverseStock | None:
    _ensure_indices_column(conn)
    row = conn.execute(
        "SELECT symbol, name, market_cap_rank, last_price, yahoo_ticker, indices "
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
        indices=_parse_indices(row["indices"] if "indices" in row.keys() else None),
    )


def _parse_nse_csv(text: str) -> list[tuple[str, str]]:
    """Return list of (symbol, company_name)."""
    reader = csv.DictReader(io.StringIO(text))
    out: list[tuple[str, str]] = []
    for row in reader:
        symbol = (row.get("Symbol") or row.get("SYMBOL") or "").strip().upper()
        name = (row.get("Company Name") or row.get("Company") or symbol).strip()
        if symbol:
            out.append((symbol, name))
    return out


def _fetch_index_csv(client: httpx.Client, filename: str) -> list[tuple[str, str]]:
    response = client.get(f"{NSE_INDEX_BASE}{filename}")
    response.raise_for_status()
    return _parse_nse_csv(response.text)


def refresh_universe(conn, settings: Settings | None = None) -> list[UniverseStock]:
    """
    Refresh from all configured NSE index CSVs.
    Rank order follows Nifty Total Market; each symbol stores membership tags.
    Falls back to bundled seed on network failure.
    """
    del settings
    seed_by_symbol = {s.symbol: s for s in load_seed_stocks()}
    try:
        membership: dict[str, set[str]] = {}
        names: dict[str, str] = {}
        order_primary: list[str] = []
        failed: list[str] = []

        with httpx.Client(headers=NSE_HEADERS, timeout=60.0, follow_redirects=True) as client:
            try:
                client.get("https://www.nseindia.com")
            except httpx.HTTPError:
                pass
            for label, filename in NSE_INDEX_FILES:
                try:
                    rows = _fetch_index_csv(client, filename)
                except Exception as exc:
                    failed.append(f"{label}: {exc}")
                    logger.warning("Index fetch failed %s: %s", label, exc)
                    continue
                if not rows:
                    failed.append(f"{label}: empty")
                    continue
                logger.info("Fetched %s: %s names", label, len(rows))
                for symbol, name in rows:
                    membership.setdefault(symbol, set()).add(label)
                    names[symbol] = name
                if label == PRIMARY_INDEX:
                    order_primary = [sym for sym, _ in rows]

        if len(order_primary) < 600:
            # Fall back: Nifty 500 members first, then remaining tagged names.
            nifty500 = [sym for sym, tags in membership.items() if "Nifty 500" in tags]
            rest = [sym for sym in membership if sym not in set(nifty500)]
            order_primary = nifty500 + sorted(rest)
        if len(order_primary) < 400:
            raise RuntimeError(
                f"unexpected universe size: {len(order_primary)}; failed={failed}"
            )

        seen: set[str] = set()
        stocks: list[UniverseStock] = []
        for symbol in order_primary:
            if symbol in seen:
                continue
            seen.add(symbol)
            seed = seed_by_symbol.get(symbol)
            stocks.append(
                UniverseStock(
                    symbol=symbol,
                    name=names.get(symbol) or (seed.name if seed else symbol),
                    market_cap_rank=len(stocks) + 1,
                    last_price=seed.last_price if seed else None,
                    yahoo_ticker=yahoo_ticker(symbol),
                    indices=sorted(membership.get(symbol, {PRIMARY_INDEX})),
                )
            )
        for symbol, tags in membership.items():
            if symbol in seen:
                continue
            seed = seed_by_symbol.get(symbol)
            stocks.append(
                UniverseStock(
                    symbol=symbol,
                    name=names.get(symbol) or (seed.name if seed else symbol),
                    market_cap_rank=len(stocks) + 1,
                    last_price=seed.last_price if seed else None,
                    yahoo_ticker=yahoo_ticker(symbol),
                    indices=sorted(tags),
                )
            )
        persist_universe(conn, stocks)
        logger.info(
            "Refreshed universe from NSE indices: %s names (failed=%s)",
            len(stocks),
            failed or "none",
        )
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
        writer.writerow(["rank", "symbol", "name", "last_price", "indices"])
        for stock in stocks:
            writer.writerow(
                [
                    stock.market_cap_rank,
                    stock.symbol,
                    stock.name,
                    stock.last_price or "",
                    _join_indices(stock.indices),
                ]
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
