from __future__ import annotations

from sqlalchemy import text
import pandas as pd

from db import get_engine, init_db
from providers import (
    fetch_profile_yf,
    fetch_revenue_yf,
    fetch_price_history_yf,
    today_utc_date,
    now_utc_ts,
)

# Starting peer set for NEE (Utilities)
PEERS_DEFAULT = ["NEE", "DUK", "SO", "AEP", "D", "EXC", "XEL", "PEG", "ED", "ETR"]


def upsert_peer(conn, profile: dict) -> None:
    q = """
    INSERT INTO peers (ticker, name, sector, industry, source, updated_at)
    VALUES (:ticker, :name, :sector, :industry, 'yfinance', :updated_at)
    ON CONFLICT(ticker) DO UPDATE SET
        name=excluded.name,
        sector=excluded.sector,
        industry=excluded.industry,
        updated_at=excluded.updated_at
    """
    conn.execute(
        text(q),
        {
            "ticker": profile["ticker"],
            "name": profile.get("name"),
            "sector": profile.get("sector"),
            "industry": profile.get("industry"),
            "updated_at": now_utc_ts(),
        },
    )


def upsert_valuation(conn, ticker: str, trailing_pe, forward_pe) -> None:
    q = """
    INSERT INTO valuation_snapshot (ticker, asof_date, trailing_pe, forward_pe, source, updated_at)
    VALUES (:ticker, :asof_date, :trailing_pe, :forward_pe, 'yfinance', :updated_at)
    ON CONFLICT(ticker, asof_date, source) DO UPDATE SET
        trailing_pe=excluded.trailing_pe,
        forward_pe=excluded.forward_pe,
        updated_at=excluded.updated_at
    """
    conn.execute(
        text(q),
        {
            "ticker": ticker,
            "asof_date": today_utc_date(),
            "trailing_pe": trailing_pe,
            "forward_pe": forward_pe,
            "updated_at": now_utc_ts(),
        },
    )


def upsert_revenue(conn, rev: pd.DataFrame, currency: str) -> int:
    if rev is None or rev.empty:
        return 0

    q = """
    INSERT OR IGNORE INTO revenue_series
    (ticker, period_type, period_end, revenue, currency, source, updated_at)
    VALUES (:ticker, :period_type, :period_end, :revenue, :currency, 'yfinance', :updated_at)
    """
    rows = rev.copy()
    rows["currency"] = currency
    rows["updated_at"] = now_utc_ts()
    conn.execute(text(q), rows.to_dict(orient="records"))
    return len(rows)


def upsert_prices(conn, prices: pd.DataFrame) -> int:
    if prices is None or prices.empty:
        return 0

    q = """
    INSERT OR IGNORE INTO price_history
    (ticker, date, adj_close, close, volume, source, updated_at)
    VALUES (:ticker, :date, :adj_close, :close, :volume, 'yfinance', :updated_at)
    """
    rows = prices.copy()
    rows["updated_at"] = now_utc_ts()
    conn.execute(text(q), rows.to_dict(orient="records"))
    return len(rows)


def refresh_peers(peers=None, years_prices: int = 10) -> dict:
    peers = peers or PEERS_DEFAULT
    peers = [p.upper().strip() for p in peers if p and p.strip()]

    engine = get_engine()
    init_db(engine)

    stats = {"tickers": 0, "rev_rows_attempted": 0, "price_rows_attempted": 0}

    with engine.begin() as conn:
        for tk in peers:
            profile = fetch_profile_yf(tk)

            upsert_peer(conn, profile)
            upsert_valuation(
                conn,
                profile["ticker"],
                profile.get("trailing_pe"),
                profile.get("forward_pe"),
            )

            rev = fetch_revenue_yf(tk)
            stats["rev_rows_attempted"] += upsert_revenue(conn, rev, profile.get("currency", "USD"))

            prices = fetch_price_history_yf(tk, years=years_prices)
            stats["price_rows_attempted"] += upsert_prices(conn, prices)

            stats["tickers"] += 1

    return stats


if __name__ == "__main__":
    print(refresh_peers())
