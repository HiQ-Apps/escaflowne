"""
data_loader.py — Databento price data for the EP backtest.

Two pulls:
  1) Daily bars for the full universe over the backtest window.
     Used for: gap %, prior-base detection, daily 10 MA exit (V1b), ADR.
  2) 1-minute bars ONLY on earnings-day-and-day-after windows.
     Used for: ORB entry, intraday volume confirmation, LoD stop.

The 1-min "windowed" pull saves a huge amount of data cost vs. pulling
1-min for every ticker for every day across 5 years.

Requires: DATABENTO_API_KEY in env, or pass api_key= explicitly.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import databento as db
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DAILY_CACHE = PROJECT_ROOT / "data" / "raw" / "daily"
MINUTE_CACHE = PROJECT_ROOT / "data" / "raw" / "minute"
DAILY_CACHE.mkdir(parents=True, exist_ok=True)
MINUTE_CACHE.mkdir(parents=True, exist_ok=True)

# Equities consolidated dataset on Databento
DATASET = "XNAS.ITCH"  # Nasdaq TotalView; for NYSE-listed names also try DBEQ.BASIC
SCHEMA_DAILY = "ohlcv-1d"
SCHEMA_MINUTE = "ohlcv-1m"


def _client(api_key: str | None = None) -> db.Historical:
    key = api_key or os.environ.get("DATABENTO_API_KEY")
    if not key:
        raise RuntimeError("DATABENTO_API_KEY not set")
    return db.Historical(key)


def fetch_daily(
    tickers: list[str],
    start: str,
    end: str,
    api_key: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Pull daily OHLCV for the universe over [start, end].
    Cached as one parquet per ticker.
    """
    frames = []
    client = None

    for ticker in tickers:
        cache_path = DAILY_CACHE / f"{ticker}_{start}_{end}.parquet"
        if use_cache and cache_path.exists():
            log.info("Cache hit (daily): %s", ticker)
            frames.append(pd.read_parquet(cache_path))
            continue

        if client is None:
            client = _client(api_key)

        log.info("Fetching daily %s [%s -> %s]", ticker, start, end)
        try:
            data = client.timeseries.get_range(
                dataset=DATASET,
                schema=SCHEMA_DAILY,
                symbols=[ticker],
                start=start,
                end=end,
                stype_in="raw_symbol",
            )
            df = data.to_df()
        except Exception as e:
            log.error("Databento error for %s: %s", ticker, e)
            continue

        if df.empty:
            log.warning("No daily data for %s", ticker)
            continue

        df = df.reset_index()
        df["ticker"] = ticker
        # Normalize timestamp to date
        ts_col = "ts_event" if "ts_event" in df.columns else df.columns[0]
        df["date"] = pd.to_datetime(df[ts_col]).dt.tz_localize(None).dt.normalize()
        keep = ["ticker", "date", "open", "high", "low", "close", "volume"]
        df = df[[c for c in keep if c in df.columns]]
        df.to_parquet(cache_path, index=False)
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["ticker", "date"]).reset_index(drop=True)


def fetch_minute_window(
    ticker: str,
    event_date: pd.Timestamp,
    days_before: int = 0,
    days_after: int = 1,
    api_key: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Pull 1-min OHLCV for a single ticker around an earnings event.
    Default window: event_date through event_date + 1 day (i.e., earnings day and day +1).
    """
    event_date = pd.Timestamp(event_date).normalize()
    start_dt = (event_date - timedelta(days=days_before)).strftime("%Y-%m-%d")
    end_dt = (event_date + timedelta(days=days_after + 1)).strftime("%Y-%m-%d")  # exclusive end

    cache_path = MINUTE_CACHE / f"{ticker}_{event_date.strftime('%Y%m%d')}.parquet"
    if use_cache and cache_path.exists():
        return pd.read_parquet(cache_path)

    client = _client(api_key)
    log.info("Fetching 1-min %s around %s", ticker, event_date.date())
    try:
        data = client.timeseries.get_range(
            dataset=DATASET,
            schema=SCHEMA_MINUTE,
            symbols=[ticker],
            start=start_dt,
            end=end_dt,
            stype_in="raw_symbol",
        )
        df = data.to_df()
    except Exception as e:
        log.error("Databento minute error for %s on %s: %s", ticker, event_date.date(), e)
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.reset_index()
    df["ticker"] = ticker
    ts_col = "ts_event" if "ts_event" in df.columns else df.columns[0]
    df["ts"] = pd.to_datetime(df[ts_col])  # keep tz-aware (UTC); convert downstream as needed
    keep = ["ticker", "ts", "open", "high", "low", "close", "volume"]
    df = df[[c for c in keep if c in df.columns]]
    df.to_parquet(cache_path, index=False)
    return df


def fetch_minute_for_events(
    events: pd.DataFrame,
    api_key: str | None = None,
) -> pd.DataFrame:
    """
    Given a DataFrame of earnings events (ticker, earnings_dt, session),
    pull 1-min bars for the day-after-earnings window:
      - AMC (after close): entry day = earnings_dt + 1 trading day
      - BMO (before open): entry day = earnings_dt itself
    For simplicity in v1 we pull a 2-day window starting at earnings_dt;
    the strategy layer picks the right entry day based on session.
    """
    frames = []
    for row in events.itertuples(index=False):
        df = fetch_minute_window(
            ticker=row.ticker,
            event_date=row.earnings_dt,
            days_before=0,
            days_after=2,  # earnings_dt, +1, +2 to safely cover BMO and AMC entry days
            api_key=api_key,
        )
        if not df.empty:
            df["earnings_dt"] = row.earnings_dt
            frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


if __name__ == "__main__":
    # Smoke test — adjust dates and tickers as needed
    UNIVERSE = ["NVDA", "TSLA", "AMD"]
    daily = fetch_daily(UNIVERSE, start="2024-01-01", end="2024-12-31")
    print(f"Daily rows: {len(daily)}")
    print(daily.head())