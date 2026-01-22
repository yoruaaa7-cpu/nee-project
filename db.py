from __future__ import annotations

from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

DB_DIR = Path("data")
DB_DIR.mkdir(exist_ok=True)
DB_FILE = DB_DIR / "utilities_peers.sqlite"


def get_engine() -> Engine:
    # check_same_thread=False helps Streamlit + SQLite work together in Codespaces
    return create_engine(
        f"sqlite:///{DB_FILE}",
        future=True,
        connect_args={"check_same_thread": False},
    )


def init_db(engine: Engine) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS peers (
        ticker TEXT PRIMARY KEY,
        name TEXT,
        sector TEXT,
        industry TEXT,
        source TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS revenue_series (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        period_type TEXT NOT NULL,     -- 'annual' | 'quarterly'
        period_end TEXT NOT NULL,      -- YYYY-MM-DD
        revenue REAL,
        currency TEXT,
        source TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(ticker, period_type, period_end, source)
    );

    CREATE TABLE IF NOT EXISTS valuation_snapshot (
        ticker TEXT NOT NULL,
        asof_date TEXT NOT NULL,       -- YYYY-MM-DD
        trailing_pe REAL,
        forward_pe REAL,
        source TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (ticker, asof_date, source)
    );

    CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        date TEXT NOT NULL,            -- YYYY-MM-DD
        adj_close REAL,
        close REAL,
        volume REAL,
        source TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(ticker, date, source)
    );

    CREATE INDEX IF NOT EXISTS idx_rev_lookup
    ON revenue_series (ticker, period_type, period_end);

    CREATE INDEX IF NOT EXISTS idx_val_lookup
    ON valuation_snapshot (ticker, asof_date);

    CREATE INDEX IF NOT EXISTS idx_price_lookup
    ON price_history (ticker, date);
    """
    with engine.begin() as conn:
        for stmt in ddl.split(";"):
            s = stmt.strip()
            if s:
                conn.execute(text(s))
