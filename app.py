from __future__ import annotations

import pandas as pd
import streamlit as st
import plotly.express as px
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from sqlalchemy import text
from sqlalchemy.engine import Engine

from db import get_engine, init_db
from ingest import refresh_peers, PEERS_DEFAULT
from metrics import compute_yoy_growth, normalize_series, compute_cagr_windows


st.set_page_config(page_title="NEE Peer Dashboard", layout="wide")


@st.cache_resource
def engine() -> Engine:
    e = get_engine()
    init_db(e)
    return e


@st.cache_data(ttl=300)
def load_peers() -> pd.DataFrame:
    q = "SELECT ticker, name, sector, industry, updated_at FROM peers ORDER BY ticker"
    with engine().connect() as conn:
        return pd.read_sql_query(text(q), conn)


@st.cache_data(ttl=300)
def load_revenue(period_type: str, tickers: list[str]) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame(columns=["ticker", "period_end", "revenue", "currency"])

    placeholders = ",".join([f":t{i}" for i in range(len(tickers))])
    q = f"""
    SELECT ticker, period_end, revenue, currency
    FROM revenue_series
    WHERE period_type = :period_type AND ticker IN ({placeholders})
    ORDER BY period_end ASC
    """
    params = {"period_type": period_type, **{f"t{i}": tk for i, tk in enumerate(tickers)}}

    with engine().connect() as conn:
        df = pd.read_sql_query(text(q), conn, params=params)

    df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    return df.dropna(subset=["period_end"])


@st.cache_data(ttl=300)
def load_prices(tickers: list[str]) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame(columns=["ticker", "date", "adj_close"])

    placeholders = ",".join([f":t{i}" for i in range(len(tickers))])
    q = f"""
    SELECT ticker, date, adj_close
    FROM price_history
    WHERE ticker IN ({placeholders})
    ORDER BY date ASC
    """
    params = {f"t{i}": tk for i, tk in enumerate(tickers)}

    with engine().connect() as conn:
        df = pd.read_sql_query(text(q), conn, params=params)

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
    return df.dropna(subset=["date"])


@st.cache_data(ttl=300)
def load_valuation_latest(tickers: list[str]) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame(columns=["ticker", "asof_date", "trailing_pe", "forward_pe"])

    placeholders = ",".join([f":t{i}" for i in range(len(tickers))])
    q = f"""
    SELECT v.ticker, v.asof_date, v.trailing_pe, v.forward_pe
    FROM valuation_snapshot v
    WHERE v.asof_date = (SELECT MAX(asof_date) FROM valuation_snapshot)
      AND v.ticker IN ({placeholders})
    """
    params = {f"t{i}": tk for i, tk in enumerate(tickers)}

    with engine().connect() as conn:
        df = pd.read_sql_query(text(q), conn, params=params)

    df["trailing_pe"] = pd.to_numeric(df["trailing_pe"], errors="coerce")
    df["forward_pe"] = pd.to_numeric(df["forward_pe"], errors="coerce")
    return df


def plot_lines(df: pd.DataFrame, x: str, y: str, group: str, xlabel: str, ylabel: str):
    fig, ax = plt.subplots()

    for g, sub in df.groupby(group):
        ax.plot(sub[x], sub[y], label=g)

    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)

    # ---- Fix messy date labels on the x-axis ----
    if pd.api.types.is_datetime64_any_dtype(df[x]):
        ax.xaxis.set_major_locator(mdates.YearLocator())              # one tick per year
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))      # format as YYYY
        fig.autofmt_xdate(rotation=0)                                 # keep clean (0 or 30)
        # If you want angled labels, use:
        # fig.autofmt_xdate(rotation=30)

    ax.legend(fontsize=8)
    fig.tight_layout()
    return fig



def plot_scatter(df: pd.DataFrame, x: str, y: str, label_col: str, xlabel: str, ylabel: str):
    fig = plt.figure()
    d = df.dropna(subset=[x, y]).copy()
    plt.scatter(d[x], d[y])
    for _, r in d.iterrows():
        plt.annotate(str(r[label_col]), (r[x], r[y]))
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    return fig


st.title("NEE FINANCIAL ANALYSIS")
st.caption("Peer comparison dashboard: revenue growth, valuation (P/E), and stock performance")


with st.sidebar:
    st.subheader("Universe")
    peers = st.multiselect("Tickers", options=sorted(set(PEERS_DEFAULT)), default=PEERS_DEFAULT)

    period_type = st.selectbox("Revenue periods", ["annual", "quarterly"], index=0)
    price_years = st.slider("Price history years to ingest", min_value=3, max_value=20, value=10)

    if st.button("Refresh / Ingest (Update DB)"):
        with st.spinner("Ingesting price + revenue + P/E into SQLite..."):
            stats = refresh_peers(peers, years_prices=price_years)

        load_peers.clear()
        load_revenue.clear()
        load_prices.clear()
        load_valuation_latest.clear()

        st.success(
            f"Updated {stats['tickers']} tickers | "
            f"Revenue rows attempted: {stats['rev_rows_attempted']} | "
            f"Price rows attempted: {stats['price_rows_attempted']}"
        )


tickers = [t.upper().strip() for t in peers]

peers_df = load_peers()
st.subheader("Companies in DB")
st.dataframe(peers_df[peers_df["ticker"].isin(tickers)], use_container_width=True)

val = load_valuation_latest(tickers)
st.subheader("Latest valuation snapshot (P/E)")
st.dataframe(val.sort_values("trailing_pe", ascending=False, na_position="last"), use_container_width=True)


# ---------------- Revenue Section ----------------
st.header("Revenue: levels + growth")

rev = load_revenue(period_type, tickers)
if rev.empty:
    st.warning("No revenue data yet. Click Refresh / Ingest.")
else:
    rev_yoy = compute_yoy_growth(rev.rename(columns={"period_end": "date"}), date_col="date", value_col="revenue")
    rev_yoy = rev_yoy.rename(columns={"date": "period_end", "yoy_growth_pct": "rev_yoy_growth_pct"})

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Revenue (level)")
        fig = plot_lines(
            rev_yoy,
            x="period_end",
            y="revenue",
            group="ticker",
            xlabel="Period end",
            ylabel="Revenue",
        )
        st.pyplot(fig)

    with c2:
        st.subheader("Revenue YoY growth (%)")
        fig = plot_lines(
            rev_yoy,
            x="period_end",
            y="rev_yoy_growth_pct",
            group="ticker",
            xlabel="Period end",
            ylabel="YoY %",
        )
        st.pyplot(fig)

    if period_type == "annual":
        st.subheader("Past revenue growth rate (Revenue CAGR %)")
        rev_cagr = compute_cagr_windows(
            rev.rename(columns={"period_end": "date"}),
            date_col="date",
            value_col="revenue",
            windows_years=(3, 5, 10),
        )
        if rev_cagr.empty:
            st.info("Not enough annual history to compute revenue CAGR windows.")
        else:
            rev_pivot = rev_cagr.pivot_table(
                index="ticker", columns="window_years", values="cagr_pct", aggfunc="first"
            ).rename(columns={3: "3Y", 5: "5Y", 10: "10Y"})
            if "5Y" in rev_pivot.columns:
                rev_pivot = rev_pivot.sort_values("5Y", ascending=False)
            st.dataframe(rev_pivot, use_container_width=True)


# ---------------- Price Section ----------------
# ----------------- Price Section -----------------
st.header("Stock performance: previous years + growth rate")

# Load price history from DB
prices = load_prices(tuple(peers))

if prices.empty:
    st.warning("No price history in DB yet. Click 'Refresh / Ingest (Update DB)'.")
else:
    # Ensure correct dtypes
    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"], errors="coerce")
    prices = prices.dropna(subset=["date"]).sort_values(["ticker", "date"])

    # --------- Normalized price chart (start=100) ----------
    prices_n = normalize_series(
    prices.rename(columns={"date": "dt"}),
    date_col="dt",
    value_col="adj_close",
    ).rename(columns={"dt": "date"})


    c1, c2 = st.columns(2)

    with c1:
        st.subheader("Normalized Adj Close (start=100)")
        fig = plot_lines(
            prices_n,
            x="date",
            y="normalized",
            group="ticker",
            xlabel="Date",
            ylabel="Normalized",
        )
        st.pyplot(fig)

    # --------- 3D / Heatmap: NEE monthly return surface ----------
    with c2:
        st.subheader("3D: NEE monthly return surface")

        nee = prices[prices["ticker"] == "NEE"].dropna(subset=["adj_close"]).copy()

        if nee.empty:
            st.info("No NEE price data available yet. Click 'Refresh / Ingest (Update DB)' to fetch prices.")
        else:
            nee = nee.set_index("date").sort_index()
            monthly = nee["adj_close"].resample("M").last().pct_change()

            surf = monthly.to_frame("ret").dropna()
            surf["year"] = surf.index.year
            surf["month"] = surf.index.month

            pivot = surf.pivot(index="year", columns="month", values="ret").sort_index()

            fig_surface = px.imshow(
                pivot,
                aspect="auto",
                labels=dict(x="Month", y="Year", color="Monthly Return"),
                title="NEE Monthly Returns Heatmap (Year × Month)",
            )
            st.plotly_chart(fig_surface, use_container_width=True)

# ---------------- Cross-metric Scatter ----------------
st.header("Valuation vs Growth (scatter)")

# Revenue CAGR 5Y (annual only) and Price CAGR 5Y
rev5 = pd.DataFrame(columns=["ticker", "rev_cagr_5y"])
price5 = pd.DataFrame(columns=["ticker", "price_cagr_5y"])

if not rev.empty and period_type == "annual":
    rev_cagr_sc = compute_cagr_windows(
        rev.rename(columns={"period_end": "date"}),
        date_col="date",
        value_col="revenue",
        windows_years=(5,),
    )
    if not rev_cagr_sc.empty:
        rev5 = rev_cagr_sc.rename(columns={"cagr_pct": "rev_cagr_5y"})[["ticker", "rev_cagr_5y"]]

if not prices.empty:
    price_cagr_sc = compute_cagr_windows(
        prices.rename(columns={"date": "dt"}),
        date_col="dt",
        value_col="adj_close",
        windows_years=(5,),
    )
    if not price_cagr_sc.empty:
        price5 = price_cagr_sc.rename(columns={"cagr_pct": "price_cagr_5y"})[["ticker", "price_cagr_5y"]]

merged = val.merge(rev5, on="ticker", how="left").merge(price5, on="ticker", how="left")
st.dataframe(merged, use_container_width=True)
st.header("3D: Valuation vs Growth (interactive)")

# Clean data for 3D plot
d3 = merged.copy()
d3 = d3.dropna(subset=["trailing_pe", "rev_cagr_5y", "price_cagr_5y"])

if d3.empty:
    st.info("Not enough data for 3D plot (need trailing P/E, revenue CAGR 5Y, and price CAGR 5Y).")
else:
    fig3d = px.scatter_3d(
        d3,
        x="trailing_pe",
        y="rev_cagr_5y",
        z="price_cagr_5y",
        color="ticker",
        hover_name="ticker",
        title="3D: Trailing P/E vs Revenue CAGR (5Y) vs Price CAGR (5Y)",
        labels={
            "trailing_pe": "Trailing P/E",
            "rev_cagr_5y": "Revenue CAGR 5Y (%)",
            "price_cagr_5y": "Price CAGR 5Y (%)",
        },
    )
    fig3d.update_traces(marker=dict(size=6))
    st.plotly_chart(fig3d, use_container_width=True)


c1, c2 = st.columns(2)
with c1:
    st.subheader("Trailing P/E vs Revenue CAGR (5Y)")
    fig = plot_scatter(
        merged,
        x="trailing_pe",
        y="rev_cagr_5y",
        label_col="ticker",
        xlabel="Trailing P/E",
        ylabel="Revenue CAGR 5Y (%)",
    )
    st.pyplot(fig)

with c2:
    st.subheader("Trailing P/E vs Price CAGR (5Y)")
    fig = plot_scatter(
        merged,
        x="trailing_pe",
        y="price_cagr_5y",
        label_col="ticker",
        xlabel="Trailing P/E",
        ylabel="Price CAGR 5Y (%)",
    )
    st.pyplot(fig)
