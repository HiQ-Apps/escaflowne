"""
strategy/indicators.py — Escaflowne (Celeri-on-MGC)

VERBATIM COPY of Celeri's indicators.py. The math is instrument-agnostic.

Key design decision (inherited from Celeri):
    Indicators are computed on a rolling 5,000-bar window rather than
    the full multi-year history. Backward-adjusted continuous contracts have
    an artificial smooth uptrend built in. Computing ADX across that entire
    history keeps it pegged near 100, reflecting the multi-year bull market
    rather than today's 5-min action. A 5,000-bar window (~17 days of 24h
    data) gives indicators clean local context with enough warmup.

    This applies even more strongly to gold, which has had a violent multi-year
    bull market (2023-2025) — backward adjustment would make the un-windowed
    ADX even more useless than on equity indices.
"""

import pandas as pd
import pandas_ta as ta


# ---------------------------------------------------------------------------
# Constants (IDENTICAL to Celeri)
# ---------------------------------------------------------------------------

EMA_FAST      = 9
EMA_SLOW      = 21
ADX_PERIOD    = 14
ATR_PERIOD    = 14
VOL_MA_PERIOD = 20

INDICATOR_WINDOW = 5000


# ---------------------------------------------------------------------------
# Rolling indicators
# ---------------------------------------------------------------------------

def _compute_rolling_indicators(df: pd.DataFrame) -> pd.DataFrame:
    n = len(df)
    result = pd.DataFrame(index=df.index, columns=[
        "ema9", "ema21", "adx", "plus_di", "minus_di", "volume_ma", "atr",
    ], dtype=float)

    starts = list(range(0, n, INDICATOR_WINDOW))
    if starts[-1] + INDICATOR_WINDOW < n:
        starts.append(n - INDICATOR_WINDOW)

    for start in starts:
        end   = min(start + INDICATOR_WINDOW, n)
        chunk = df.iloc[start:end]

        ema9      = ta.ema(chunk["close"],  length=EMA_FAST)
        ema21     = ta.ema(chunk["close"],  length=EMA_SLOW)
        volume_ma = ta.sma(chunk["volume"], length=VOL_MA_PERIOD)
        adx_df    = ta.adx(chunk["high"], chunk["low"], chunk["close"], length=ADX_PERIOD)
        atr       = ta.atr(chunk["high"], chunk["low"], chunk["close"], length=ATR_PERIOD)

        adx      = adx_df[f"ADX_{ADX_PERIOD}"]
        plus_di  = adx_df[f"DMP_{ADX_PERIOD}"]
        minus_di = adx_df[f"DMN_{ADX_PERIOD}"]

        warmup    = 50
        valid_idx = chunk.index[warmup:]

        result.loc[valid_idx, "ema9"]      = ema9.loc[valid_idx].values
        result.loc[valid_idx, "ema21"]     = ema21.loc[valid_idx].values
        result.loc[valid_idx, "volume_ma"] = volume_ma.loc[valid_idx].values
        result.loc[valid_idx, "adx"]       = adx.loc[valid_idx].values
        result.loc[valid_idx, "plus_di"]   = plus_di.loc[valid_idx].values
        result.loc[valid_idx, "minus_di"]  = minus_di.loc[valid_idx].values
        result.loc[valid_idx, "atr"]       = atr.loc[valid_idx].values

    return pd.concat([df, result], axis=1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all strategy indicators to a 5-min OHLCV DataFrame."""
    print("Computing indicators...")
    df = _compute_rolling_indicators(df)

    nan_rows = df[["ema9", "adx", "volume_ma", "atr"]].isna().any(axis=1).sum()
    print(f"  Warmup bars (NaN): {nan_rows:,}")
    print(f"  Tradeable bars:    {len(df) - nan_rows:,}")
    print(f"  Indicators done")

    return df


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from backtest.data_loader import load_mgc

    df = load_mgc()
    df = add_indicators(df)

    sample = df[["close", "ema9", "ema21", "adx", "atr", "volume", "volume_ma"]].dropna()

    print("\nSample (last 20 bars):")
    pd.set_option("display.float_format", "{:.2f}".format)
    pd.set_option("display.width", 120)
    print(sample.tail(20).to_string())

    print(f"\nADX range: {df['adx'].min():.1f} – {df['adx'].max():.1f}")
    print(f"ATR range: {df['atr'].min():.2f} – {df['atr'].max():.2f}")
    print(f"EMA9 lag (last bar): {abs(df['close'].iloc[-1] - df['ema9'].iloc[-1]):.2f} pts")