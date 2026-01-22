from __future__ import annotations

import datetime as dt
import pandas as pd
import yfinance as yf


def today_utc_date() -> str:
    return dt.datetime.utcnow().date().isoformat()


def now_utc_ts() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


def fetch_profile_yf(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = getattr(t, "info", {}) or {}

    return {
        "ticker": ticker.upper(),
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "currency": info.get("currency", "USD"),
    }


def fetch_revenue_yf(ticker: str) -> pd.DataFrame:
    """
    Pull revenue from yfinance income statement tables:
      - annual: t.financials
      - quarterly: t.quarterly_financials

    Returns long dataframe:
      ticker | period_type | period_end | revenue
    """
    t = yf.Ticker(ticker)

    annual = getattr(t, "financials", pd.DataFrame())
    quarterly = getattr(t, "quarterly_financials", pd.DataFrame())

    def extract_revenue(df: pd.DataFrame, period_type: str) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=["ticker", "period_type", "period_end", "revenue"])

        # yfinance uses different labels depending on ticker/version
        candidates = [
            "Total Revenue",
            "TotalRevenue",
            "Revenue",
            "Operating Revenue",
        ]

        row = None
        for c in candidates:
            if c in df.index:
                row = c
                break

        if row is None:
            # no revenue row found; fail gracefully
            return pd.DataFrame(columns=["ticker", "period_type", "period_end", "revenue"])

        s = df.loc[row]

        out = (
            s.rename("revenue")
             .to_frame()
             .reset_index()
             .rename(columns={"index": "period_end"})
        )

        out["period_end"] = pd.to_datetime(out["period_end"]).dt.date.astype(str)
        out["ticker"] = ticker.upper()
        out["period_type"] = period_type
        out["revenue"] = pd.to_numeric(out["revenue"], errors="coerce")

        return out[["ticker", "period_type", "period_end", "revenue"]]

    rev_a = extract_revenue(annual, "annual")
    rev_q = extract_revenue(quarterly, "quarterly")

    return pd.concat([rev_a, rev_q], ignore_index=True)


def fetch_price_history_yf(ticker: str, years: int = 10) -> pd.DataFrame:
    """
    Daily price history using Yahoo via yfinance.
    Handles MultiIndex columns that sometimes appear from yf.download().
    """
    tkr = ticker.upper()
    end = pd.Timestamp.utcnow().date()
    start = (pd.Timestamp.utcnow() - pd.DateOffset(years=years)).date()

    df = yf.download(
        tkr,
        start=str(start),
        end=str(end),
        auto_adjust=False,
        progress=False,
    )

    if df is None or df.empty:
        return pd.DataFrame(columns=["ticker", "date", "adj_close", "close", "volume"])

    # ---- FIX: handle MultiIndex columns (e.g., ('Adj Close','NEE')) ----
    if isinstance(df.columns, pd.MultiIndex):
        lvl1 = df.columns.get_level_values(1)
        if tkr in lvl1:
            df = df.xs(tkr, axis=1, level=1, drop_level=True)
        else:
            # fallback: take the first ticker in the MultiIndex
            df = df.xs(lvl1[0], axis=1, level=1, drop_level=True)

    out = df.reset_index()

    # Sometimes yfinance doesn't include "Adj Close" in some outputs
    if "Adj Close" in out.columns:
        out = out.rename(columns={"Date": "date", "Adj Close": "adj_close", "Close": "close", "Volume": "volume"})
    else:
        # fallback: use Close as adj_close proxy if Adj Close missing
        out = out.rename(columns={"Date": "date", "Close": "adj_close", "Volume": "volume"})
        out["close"] = out["adj_close"]

    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date.astype(str)
    out["ticker"] = tkr

    # Ensure these are Series, not DataFrames
    out["adj_close"] = pd.to_numeric(out["adj_close"], errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce")

    return out[["ticker", "date", "adj_close", "close", "volume"]]

