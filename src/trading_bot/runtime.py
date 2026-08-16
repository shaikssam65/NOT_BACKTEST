from __future__ import annotations

import logging
from pathlib import Path

from trading_bot.config import Settings, load_settings
from trading_bot.data_provider import CompositeDataProvider, KiteProvider, SqliteCache, YahooProvider
from trading_bot.db import init_db
from trading_bot.universe import get_universe, load_seed_stocks, persist_universe

logger = logging.getLogger(__name__)


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def boot(settings: Settings | None = None, db_path: Path | None = None):
    settings = settings or load_settings()
    path = db_path or settings.database_path
    conn = init_db(path)
    stocks = get_universe(conn)
    # Upgrade legacy / smaller DBs to Total Market seed (~750 with index tags).
    if len(stocks) < 700 or not any(s.indices for s in stocks[:5]):
        logger.info("Upgrading universe seed to Total Market (%s → ~750)", len(stocks))
        persist_universe(conn, load_seed_stocks())
    return settings, conn


def build_provider(settings: Settings, conn) -> CompositeDataProvider:
    kite = None
    if settings.kite_ready:
        try:
            kite = KiteProvider(settings.kite_api_key, settings.kite_access_token)  # type: ignore[arg-type]
        except Exception:
            logger.exception("Kite provider init failed; Yahoo will be used")
    return CompositeDataProvider(
        cache=SqliteCache(conn),
        kite=kite,
        yahoo=YahooProvider(),
    )
