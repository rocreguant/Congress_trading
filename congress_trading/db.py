"""SQLite storage for members, filings, trades and prices."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    bioguide_id TEXT PRIMARY KEY,
    first_name  TEXT,
    last_name   TEXT,
    full_name   TEXT,
    chamber     TEXT,          -- 'house' | 'senate' (current chamber)
    state       TEXT,
    district    INTEGER,
    party       TEXT
);

CREATE TABLE IF NOT EXISTS filings (
    doc_id       TEXT PRIMARY KEY,   -- House DocID or Senate report UUID
    chamber      TEXT NOT NULL,
    bioguide_id  TEXT,
    filer_name   TEXT,
    state_dst    TEXT,
    filing_date  TEXT,               -- ISO date
    year         INTEGER,
    url          TEXT,
    status       TEXT,               -- parsed | scanned | error | pending
    n_trades     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id            TEXT NOT NULL REFERENCES filings(doc_id),
    bioguide_id       TEXT,
    chamber           TEXT,
    owner             TEXT,          -- SP (spouse), JT (joint), DC (child), self...
    asset             TEXT,
    ticker            TEXT,
    asset_type        TEXT,          -- ST (stock), OP (option), ...
    tx_type           TEXT,          -- 'buy' | 'sell' | 'exchange'
    tx_date           TEXT,          -- ISO date of the trade
    notification_date TEXT,          -- ISO date the trade was disclosed
    amount_min        REAL,
    amount_max        REAL,
    description       TEXT,
    UNIQUE(doc_id, ticker, tx_type, tx_date, amount_min, owner, asset)
);
CREATE INDEX IF NOT EXISTS ix_trades_member ON trades(bioguide_id);
CREATE INDEX IF NOT EXISTS ix_trades_ticker ON trades(ticker);

CREATE TABLE IF NOT EXISTS prices (
    ticker   TEXT NOT NULL,
    date     TEXT NOT NULL,
    adjclose REAL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS price_fetch (
    ticker     TEXT PRIMARY KEY,
    status     TEXT,                 -- ok | missing
    fetched_at TEXT
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con
