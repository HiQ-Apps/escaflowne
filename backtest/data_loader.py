"""
backtest/data_loader.py — Escaflowne (Celeri-on-MGC)

Cloned from Celeri's data_loader.py with two adaptations:
  1. Symbol prefix: "MGC" instead of "MES"/"MNQ"
  2. Roll schedule: gold rolls Feb/Apr/Jun/Aug/Oct/Dec on the third-to-last
     business day of the contract month — NOT the third Friday quarterly
     schedule that equity index futures follow.

Pipeline (identical to Celeri):
    1. Load raw CSV (1-min bars from Databento)
    2. Fix timestamps -> ET
    3. Filter to MGC symbols
    4. Build continuous contract (front-month by volume, backward price adjust)
    5a. Clean 1-min bars (drop zero volume)
    5b. Resample to 5-min OHLCV
    6. Remove roll weeks (5 days before each contract's last trading day)
    7. Validate and save

Run once after downloading data:
    python -m backtest.data_loader data/raw/mgc.csv.zst

Then precompute indicators (5-min only):
    python -c "from backtest.data_loader import precompute_indicators; precompute_indicators()"
"""

import pandas as pd
import numpy as np
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROCESSED_DIR = Path("data/processed")
MGC_FILE      = PROCESSED_DIR / "MGC_5min.parquet"
MGC_1MIN_FILE = PROCESSED_DIR / "MGC_1min.parquet"
MGC_IND_FILE  = PROCESSED_DIR / "MGC_5min_ind.parquet"

# Gold contract months (every 2 months, not the equity quarterly schedule)
EXPIRY_MONTHS = [2, 4, 6, 8, 10, 12]


# ---------------------------------------------------------------------------
# Step 1: Load raw CSV
# ---------------------------------------------------------------------------

def _load_raw(filepath: str | Path) -> pd.DataFrame:
    filepath = Path(filepath)
    print(f"Reading {filepath.name} ...")
    df = pd.read_csv(
        filepath,
        compression="zstd",
        usecols=["ts_event", "symbol", "open", "high", "low", "close", "volume"],
    )
    print(f"  Raw rows: {len(df):,}")
    return df


# ---------------------------------------------------------------------------
# Step 2: Fix timestamps
# ---------------------------------------------------------------------------

def _fix_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    df["ts_event"] = pd.to_datetime(df["ts_event"], unit="ns", utc=True)
    df["ts_event"] = df["ts_event"].dt.tz_convert("America/New_York")
    df = df.set_index("ts_event")
    df.index.name = "datetime"
    return df


# ---------------------------------------------------------------------------
# Step 3: Filter to MGC symbols
# ---------------------------------------------------------------------------

def _filter_mgc(df: pd.DataFrame) -> pd.DataFrame:
    mgc = df[df["symbol"].str.startswith("MGC")].copy()
    print(f"  MGC rows: {len(mgc):,}")
    if len(mgc) == 0:
        raise ValueError("No MGC rows found. Check that the raw CSV contains MGC symbols.")
    return mgc


# ---------------------------------------------------------------------------
# Step 4: Build continuous contract (IDENTICAL to Celeri's logic)
# ---------------------------------------------------------------------------

def _build_continuous(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    print(f"  Building continuous contract for {symbol}...")

    df["_date"] = df.index.date
    daily_volume = (
        df.groupby(["_date", "symbol"])["volume"]
          .sum()
          .reset_index()
    )
    front_by_date = (
        daily_volume
        .sort_values("volume", ascending=False)
        .drop_duplicates(subset="_date", keep="first")
        .set_index("_date")["symbol"]
        .rename("front_contract")
    )
    df["front_contract"] = df["_date"].map(front_by_date)
    df = df.drop(columns=["_date"])

    df_front = df[df["symbol"] == df["front_contract"]].copy()
    df_front = df_front.drop(columns=["front_contract"])
    df_front = df_front.sort_index()
    df_front = df_front[~df_front.index.duplicated(keep="first")]
    print(f"    Front-month rows selected: {len(df_front):,}")

    prev_symbol  = df_front["symbol"].shift(1)
    roll_mask    = (df_front["symbol"] != prev_symbol) & prev_symbol.notna()
    roll_indices = df_front.index[roll_mask].tolist()
    print(f"    Roll events detected: {len(roll_indices)}")

    price_cols = ["open", "high", "low", "close"]
    prices     = df_front[price_cols].values.copy()
    idx_array  = df_front.index
    cumulative = 0.0

    for roll_time in reversed(roll_indices):
        pos = idx_array.get_loc(roll_time)
        if pos == 0:
            continue
        gap = float(prices[pos, 3]) - float(prices[pos - 1, 3])
        cumulative += gap
        prices[:pos] -= gap

    df_front[price_cols] = prices
    df_front = df_front.drop(columns=["symbol"])
    print(f"    Cumulative price adjustment: {cumulative:.2f} points")
    return df_front


# ---------------------------------------------------------------------------
# Step 5a: Clean 1-min bars
# ---------------------------------------------------------------------------

def _clean_1min(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["open", "high", "low", "close", "volume"]].copy()
    df = df.dropna()
    df = df[df["volume"] > 0]
    return df


# ---------------------------------------------------------------------------
# Step 5b: Resample to 5-min
# ---------------------------------------------------------------------------

def _resample_to_5min(df: pd.DataFrame) -> pd.DataFrame:
    df_5m = df[["open", "high", "low", "close", "volume"]].resample(
        "5min", label="left", closed="left"
    ).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    })
    df_5m = df_5m.dropna()
    df_5m = df_5m[df_5m["volume"] > 0]
    return df_5m


# ---------------------------------------------------------------------------
# Step 6: Remove roll weeks (gold-specific schedule)
# ---------------------------------------------------------------------------

def _last_trading_day_of_month(year: int, month: int) -> pd.Timestamp:
    """
    Gold contract's last trading day = third-to-last business day of contract month.
    (CME spec: trading terminates on the third-to-last business day of the
    contract month at 13:30 ET.)
    """
    # Get all business days in the month
    month_start = pd.Timestamp(year=year, month=month, day=1)
    if month == 12:
        next_month_start = pd.Timestamp(year=year + 1, month=1, day=1)
    else:
        next_month_start = pd.Timestamp(year=year, month=month + 1, day=1)

    all_bdays = pd.bdate_range(start=month_start, end=next_month_start - pd.Timedelta(days=1))
    if len(all_bdays) < 3:
        return all_bdays[-1] if len(all_bdays) else month_start
    return all_bdays[-3]  # third-to-last business day


def _remove_roll_weeks(df: pd.DataFrame) -> pd.DataFrame:
    """Remove 5 trading days before each gold contract's last trading day."""
    years = df.index.year.unique()
    exclude_dates = set()

    for year in years:
        for month in EXPIRY_MONTHS:
            ltd = _last_trading_day_of_month(year, month)
            count = 0
            day = ltd - pd.Timedelta(days=1)
            while count < 5:
                if day.weekday() < 5:
                    exclude_dates.add(day.date())
                    count += 1
                day -= pd.Timedelta(days=1)
            # Also exclude the last trading day itself
            exclude_dates.add(ltd.date())

    exclude_mask = pd.Series(
        [d in exclude_dates for d in df.index.date],
        index=df.index
    )
    filtered = df[~exclude_mask]
    print(f"  Bars removed (roll weeks): {len(df) - len(filtered):,}")
    return filtered.copy()


# ---------------------------------------------------------------------------
# Step 7: Validate and save
# ---------------------------------------------------------------------------

def _validate(df: pd.DataFrame, symbol: str) -> None:
    assert len(df) > 0,                       f"{symbol}: empty!"
    assert df.index.is_monotonic_increasing,  f"{symbol}: index not sorted!"
    assert not df.isnull().any().any(),       f"{symbol}: contains NaN!"
    assert (df["high"] >= df["low"]).all(),   f"{symbol}: high < low!"
    assert (df["volume"] > 0).all(),          f"{symbol}: zero volume bars!"
    print(f"  ✅ {symbol} validation passed")


def _save(df: pd.DataFrame, path: Path, symbol: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    print(f"  💾 {symbol} saved -> {path}  ({len(df):,} bars)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_and_save(filepath: str | Path) -> None:
    print(f"\n{'='*55}")
    print("Escaflowne — Data Processing Pipeline (MGC)")
    print(f"{'='*55}")

    df_raw = _load_raw(filepath)
    df_raw = _fix_timestamps(df_raw)
    df_mgc_raw = _filter_mgc(df_raw)

    print(f"\nProcessing MGC...")
    df_continuous = _build_continuous(df_mgc_raw, symbol="MGC")

    # 1-min — engine heartbeat
    df_1m = df_continuous.pipe(_clean_1min).pipe(_remove_roll_weeks)
    _validate(df_1m, "MGC 1min")
    _save(df_1m, MGC_1MIN_FILE, "MGC 1min")

    # 5-min — signal generation
    df_5m = df_continuous.pipe(_resample_to_5min).pipe(_remove_roll_weeks)
    _validate(df_5m, "MGC 5min")
    print(f"  Date range: {df_5m.index[0]} -> {df_5m.index[-1]}")
    _save(df_5m, MGC_FILE, "MGC 5min")

    print(f"\n{'='*55}")
    print("Done! Next step:")
    print("  python -c \"from backtest.data_loader import precompute_indicators; precompute_indicators()\"")
    print(f"{'='*55}\n")


def precompute_indicators() -> None:
    from strategy.indicators import add_indicators

    print(f"Computing indicators for MGC 5min...", end=" ", flush=True)
    df = add_indicators(pd.read_parquet(MGC_FILE))
    df.to_parquet(MGC_IND_FILE)
    print(f"saved -> {MGC_IND_FILE.name}  ({len(df):,} bars)")
    print("\nDone! Use load_mgc_fast() in backtests.")


def load_mgc() -> pd.DataFrame:
    assert MGC_FILE.exists(), "Run process_and_save() first."
    return pd.read_parquet(MGC_FILE)


def load_mgc_1min() -> pd.DataFrame:
    assert MGC_1MIN_FILE.exists(), "Run process_and_save() first."
    return pd.read_parquet(MGC_1MIN_FILE)


def load_mgc_fast() -> pd.DataFrame:
    if not MGC_IND_FILE.exists():
        raise FileNotFoundError(
            "Run: python -c \"from backtest.data_loader import precompute_indicators; precompute_indicators()\""
        )
    return pd.read_parquet(MGC_IND_FILE)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m backtest.data_loader <path_to_csv_zst>")
        sys.exit(1)

    process_and_save(sys.argv[1])

    print("\nVerifying MGC 5min..."); print(load_mgc().tail(3))
    print("\nVerifying MGC 1min..."); print(load_mgc_1min().tail(3))