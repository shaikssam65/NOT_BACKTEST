from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Protocol

import pandas as pd

from trading_bot.universe import yahoo_ticker

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


class HistoricalDataProvider(Protocol):
    def get_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if "date" not in out.columns:
        out = out.reset_index()
        out.columns = [str(c).lower() for c in out.columns]
        if "datetime" in out.columns and "date" not in out.columns:
            out = out.rename(columns={"datetime": "date"})
        if "index" in out.columns and "date" not in out.columns:
            out = out.rename(columns={"index": "date"})
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(out.columns)
    if missing:
        raise ValueError(f"OHLCV missing columns: {sorted(missing)}")
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None).dt.normalize()
    out = out[["date", "open", "high", "low", "close", "volume"]]
    out = out.dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0).astype("int64")
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    return out.reset_index(drop=True)


class SqliteCache:
    def __init__(self, conn) -> None:
        self.conn = conn

    def load(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        rows = self.conn.execute(
            """
            SELECT date, open, high, low, close, volume
            FROM ohlcv
            WHERE symbol = ? AND date >= ? AND date <= ?
            ORDER BY date
            """,
            (symbol.upper(), start.isoformat(), end.isoformat()),
        ).fetchall()
        if not rows:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = pd.DataFrame([dict(r) for r in rows])
        return _normalize(df)

    def save(self, symbol: str, df: pd.DataFrame, source: str) -> None:
        if df.empty:
            return
        payload = [
            (
                symbol.upper(),
                pd.Timestamp(row.date).date().isoformat(),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                int(row.volume),
                source,
            )
            for row in df.itertuples(index=False)
        ]
        self.conn.executemany(
            """
            INSERT INTO ohlcv (symbol, date, open, high, low, close, volume, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                source=excluded.source
            """,
            payload,
        )
        self.conn.commit()


class YahooProvider:
    def get_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        import yfinance as yf

        ticker = yahoo_ticker(symbol)
        # yfinance `end` is exclusive for daily bars.
        end_inclusive = end + timedelta(days=1)
        df = yf.download(
            ticker,
            start=start.isoformat(),
            end=end_inclusive.isoformat(),
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [col[0] for col in df.columns]
        df = df.reset_index()
        rename = {c: c.lower() for c in df.columns}
        df = df.rename(columns=rename)
        if "adj close" in df.columns and "close" not in df.columns:
            df["close"] = df["adj close"]
        return _normalize(df)


class KiteProvider:
    def __init__(self, api_key: str, access_token: str) -> None:
        from kiteconnect import KiteConnect

        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)
        self._tokens: dict[str, int] | None = None

    def _instrument_token(self, symbol: str) -> int | None:
        if self._tokens is None:
            instruments = self.kite.instruments("NSE")
            self._tokens = {
                str(item["tradingsymbol"]).upper(): int(item["instrument_token"])
                for item in instruments
                if item.get("segment") == "NSE" and item.get("instrument_type") == "EQ"
            }
        return self._tokens.get(symbol.upper())

    def get_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        token = self._instrument_token(symbol)
        if token is None:
            logger.warning("No NSE EQ instrument token for %s", symbol)
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        rows = self.kite.historical_data(token, start, end, "day", oi=False)
        if not rows:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = pd.DataFrame(rows)
        df = df.rename(columns={"date": "date"})
        return _normalize(df)


class CompositeDataProvider:
    """Cache -> Kite (if configured) -> Yahoo Finance."""

    def __init__(
        self,
        cache: SqliteCache,
        kite: KiteProvider | None = None,
        yahoo: YahooProvider | None = None,
    ) -> None:
        self.cache = cache
        self.kite = kite
        self.yahoo = yahoo or YahooProvider()

    def get_ohlcv(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        cached = self.cache.load(symbol, start, end)
        if _covers(cached, start, end):
            return cached
        fetched = pd.DataFrame(columns=OHLCV_COLUMNS)
        source = "yahoo"
        if self.kite is not None:
            try:
                fetched = self.kite.get_ohlcv(symbol, start, end)
                source = "kite"
            except Exception:
                logger.exception("Kite historical fetch failed for %s; trying Yahoo", symbol)
        if fetched.empty:
            try:
                fetched = self.yahoo.get_ohlcv(symbol, start, end)
                source = "yahoo"
            except Exception:
                logger.exception("Yahoo historical fetch failed for %s", symbol)
                return cached
        if not fetched.empty:
            self.cache.save(symbol, fetched, source=source)
            return self.cache.load(symbol, start, end)
        return cached


def _covers(df: pd.DataFrame, start: date, end: date) -> bool:
    if df.empty:
        return False
    first = pd.Timestamp(df["date"].iloc[0]).date()
    last = pd.Timestamp(df["date"].iloc[-1]).date()
    # Cached history is good enough if it starts within 5 sessions of `start`
    # and reaches the requested end (or last weekday if end is a weekend).
    return first <= start + timedelta(days=10) and last >= end - timedelta(days=4)


def last_n_bars(df: pd.DataFrame, n: int = 10) -> list[dict]:
    if df.empty:
        return []
    tail = df.tail(n)
    records = []
    for row in tail.itertuples(index=False):
        records.append(
            {
                "date": pd.Timestamp(row.date).date().isoformat(),
                "open": round(float(row.open), 4),
                "high": round(float(row.high), 4),
                "low": round(float(row.low), 4),
                "close": round(float(row.close), 4),
                "volume": int(row.volume),
            }
        )
    return records


def as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
