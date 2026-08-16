from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market_cap_rank INTEGER NOT NULL,
    last_price REAL,
    yahoo_ticker TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ohlcv (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_date ON ohlcv(symbol, date);

CREATE TABLE IF NOT EXISTS daily_selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    selection_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('ai_selected', 'manual')),
    rule_signal TEXT,
    ai_signal TEXT,
    combined_signal TEXT,
    confidence INTEGER,
    entry_price_target REAL,
    stop_loss_pct REAL,
    target_pct REAL,
    reasoning TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (selection_date, symbol)
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    as_of_date TEXT,
    model TEXT NOT NULL,
    source TEXT NOT NULL,
    input_json TEXT NOT NULL,
    raw_response TEXT,
    signal TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    stop_loss_pct REAL,
    target_pct REAL,
    reasoning TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_symbol ON ai_decisions(symbol, created_at);

CREATE TABLE IF NOT EXISTS backtest_cache (
    cache_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    capital REAL NOT NULL,
    results_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS universe_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Reserved for Phase 3 / 4. Created now so later modules do not migrate blindly.
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL,
    stop_loss REAL,
    target REAL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    reason TEXT,
    risk_check TEXT,
    broker_order_id TEXT
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    symbol TEXT,
    passed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    exit_time TEXT,
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    stop_loss REAL NOT NULL,
    target REAL,
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    strategy TEXT,
    source TEXT,
    pnl REAL,
    exit_reason TEXT,
    broker_order_id TEXT,
    broker_exit_order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol, status);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
