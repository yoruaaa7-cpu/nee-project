from __future__ import annotations

import pandas as pd


def compute_yoy_growth(
    df: pd.DataFrame,
    date_col: str,
    value_col: str,
    group_col: str = "ticker",
) -> pd.DataFrame:
    """
    Adds column: yoy_growth_pct (percent change between consecutive periods per ticker)
    Works for annual or quarterly series as long as sorted by date.
    """
    out = df.copy()
    out = out.sort_values([group_col, date_col])
    out["yoy_growth_pct"] = out.groupby(group_col)[value_col].pct_change() * 100.0
    return out


def normalize_series(
    df: pd.DataFrame,
    date_col: str,
    value_col: str,
    group_col: str = "ticker",
) -> pd.DataFrame:
    """
    Adds column: normalized (starts at 100 for each ticker, based on first value)
    """
    out = df.copy()
    out = out.sort_values([group_col, date_col])

    def _norm(s: pd.Series) -> pd.Series:
        if s.empty:
            return s
        first = s.iloc[0]
        if first is None or pd.isna(first) or first == 0:
            return pd.Series([pd.NA] * len(s), index=s.index)
        return (s / first) * 100.0

    out["normalized"] = out.groupby(group_col)[value_col].transform(_norm)
    return out


def compute_cagr_windows(
    df: pd.DataFrame,
    date_col: str,
    value_col: str,
    windows_years=(1, 3, 5, 10),
    group_col: str = "ticker",
) -> pd.DataFrame:
    """
    For each ticker:
      - end = last available value
      - start = first value within window (end_date - window_years)
      CAGR% = ((end/start)^(1/years) - 1) * 100
    """
    out_rows = []

    dfx = df.dropna(subset=[value_col]).copy()
    if dfx.empty:
        return pd.DataFrame(columns=[group_col, "window_years", "cagr_pct"])

    dfx[date_col] = pd.to_datetime(dfx[date_col], errors="coerce")
    dfx = dfx.dropna(subset=[date_col])

    for tk, g in dfx.groupby(group_col):
        g = g.sort_values(date_col)
        if len(g) < 2:
            continue

        end_date = g[date_col].iloc[-1]
        end_val = g[value_col].iloc[-1]
        if end_val is None or pd.isna(end_val) or end_val <= 0:
            continue

        for y in windows_years:
            start_cut = end_date - pd.DateOffset(years=int(y))
            g2 = g[g[date_col] >= start_cut]
            if len(g2) < 2:
                continue

            start_val = g2[value_col].iloc[0]
            if start_val is None or pd.isna(start_val) or start_val <= 0:
                continue

            cagr = ((end_val / start_val) ** (1.0 / float(y)) - 1.0) * 100.0
            out_rows.append({group_col: tk, "window_years": int(y), "cagr_pct": float(cagr)})

    return pd.DataFrame(out_rows)
