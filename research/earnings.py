"""
earnings.py — pull and cache historical earnings dates for the EP universe.

Uses yfinance to get earnings dates with BMO/AMC flag inferred from the
reporting time. Caches to parquet so we don't re-hit the API on every run.

For v1 we accept yfinance's coverage limitations. If the edge materializes,
we upgrade to EODHD or similar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Resolve cache dir relative to this file so the script works from anywhere
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "raw" / "earnings"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _classify_session(ts: pd.Timestamp) -> str:
    """
    Classify an earnings timestamp as BMO (before market open),
    AMC (after market close), or DMH (during market hours / unknown).

    yfinance returns timestamps in US/Eastern. Companies typically report:
      - BMO: before 9:30 ET (commonly 6:00-8:30 AM)
      - AMC: after 16:00 ET (commonly 16:00-18:00)
      - DMH/unknown: during the session (rare; usually a data quirk)
    """
    if ts is None or pd.isna(ts):
        return "UNKNOWN"
    # yfinance ts is tz-aware in US/Eastern; if not, assume Eastern
    if ts.tzinfo is None:
        ts = ts.tz_localize("US/Eastern")
    else:
        ts = ts.tz_convert("US/Eastern")
    hour = ts.hour
    minute = ts.minute
    minutes_from_midnight = hour * 60 + minute
    if minutes_from_midnight < 9 * 60 + 30:
        return "BMO"
    if minutes_from_midnight >= 16 * 60:
        return "AMC"
    return "DMH"


def fetch_earnings_for_ticker(
    ticker: str,
    limit: int = 80,  # ~20 years of quarterly earnings
) -> pd.DataFrame:
    """
    Fetch historical earnings dates for a single ticker via yfinance.

    Returns a DataFrame with columns:
      ticker, earnings_dt, session, eps_estimate, eps_actual, surprise_pct
    where earnings_dt is the calendar date (not timestamp), and session is
    BMO/AMC/DMH/UNKNOWN.
    """
    log.info("Fetching earnings for %s ...", ticker)
    try:
        t = yf.Ticker(ticker)
        df = t.get_earnings_dates(limit=limit)
    except Exception as e:
        log.warning("yfinance error for %s: %s", ticker, e)
        return pd.DataFrame()

    if df is None or df.empty:
        log.warning("No earnings data returned for %s", ticker)
        return pd.DataFrame()

    df = df.reset_index().rename(columns={"Earnings Date": "earnings_ts"})

    # Classify session before stripping time
    df["session"] = df["earnings_ts"].apply(_classify_session)
    df["earnings_dt"] = pd.to_datetime(df["earnings_ts"]).dt.tz_localize(None).dt.normalize()

    df["ticker"] = ticker

    # Standardize column names — yfinance shape has shifted historically
    rename = {}
    for c in df.columns:
        lc = c.lower()
        if "eps estimate" in lc:
            rename[c] = "eps_estimate"
        elif "reported eps" in lc:
            rename[c] = "eps_actual"
        elif "surprise" in lc:
            rename[c] = "surprise_pct"
    df = df.rename(columns=rename)

    keep = ["ticker", "earnings_dt", "session", "eps_estimate", "eps_actual", "surprise_pct"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].sort_values("earnings_dt").reset_index(drop=True)
    return df


def fetch_universe(tickers: list[str], use_cache: bool = True) -> pd.DataFrame:
    """
    Fetch earnings for all tickers, caching one parquet per ticker.
    Returns the concatenated DataFrame.
    """
    frames = []
    for ticker in tickers:
        cache_path = CACHE_DIR / f"{ticker}.parquet"
        if use_cache and cache_path.exists():
            log.info("Cache hit: %s", ticker)
            frames.append(pd.read_parquet(cache_path))
            continue

        df = fetch_earnings_for_ticker(ticker)
        if not df.empty:
            df.to_parquet(cache_path, index=False)
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["ticker", "earnings_dt"]).reset_index(drop=True)
    return combined


def filter_to_window(
    df: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DataFrame:
    """Filter earnings events to a date window for backtest scoping."""
    start = pd.to_datetime(start)
    end = pd.to_datetime(end)
    mask = (df["earnings_dt"] >= start) & (df["earnings_dt"] <= end)
    return df.loc[mask].reset_index(drop=True)


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Quick sanity-check summary by ticker."""
    if df.empty:
        return pd.DataFrame()
    g = df.groupby("ticker").agg(
        n_events=("earnings_dt", "count"),
        first=("earnings_dt", "min"),
        last=("earnings_dt", "max"),
        n_bmo=("session", lambda s: (s == "BMO").sum()),
        n_amc=("session", lambda s: (s == "AMC").sum()),
        n_unknown=("session", lambda s: s.isin(["DMH", "UNKNOWN"]).sum()),
    )
    return g


if __name__ == "__main__":
    # v1 universe — high-beta names with frequent EP candidates
    UNIVERSE = [
        "NVDA", "TSLA", "AMD", "META", "GOOGL",
        "AMZN", "NFLX", "AVGO", "CRWD", "PLTR",
        "SMCI", "COIN", "MSTR", "AAPL", "MSFT",
    ]

    df = fetch_universe(UNIVERSE)
    df = filter_to_window(df, "2022-01-01", "2025-12-31")

    print("\n=== Summary by ticker ===")
    print(summarize(df))

    print("\n=== Sample events (most recent 20) ===")
    print(df.sort_values("earnings_dt", ascending=False).head(20).to_string(index=False))

    out = PROJECT_ROOT / "data" / "processed" / "earnings_universe.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(f"\nWrote {len(df)} events to {out}")