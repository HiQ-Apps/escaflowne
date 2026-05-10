"""
research/diagnose.py — sanity check the backtest results

Looking for: lookahead bias, same-bar exits, magic timing, equity curve weirdness.
"""
import pandas as pd
from pathlib import Path

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)

# ---------------------------------------------------------------------------
# 1. Indicators sanity-check (proper sampling this time)
# ---------------------------------------------------------------------------

print("="*70)
print("1. INDICATOR INSPECTION")
print("="*70)

df = pd.read_parquet("data/processed/MGC_5min_ind.parquet")
print(f"Date range: {df.index[0]} -> {df.index[-1]}")
print(f"Total bars: {len(df):,}")
nan_per = df[["ema9","ema21","adx","atr"]].isna().sum()
print(f"NaN bars per indicator: {dict(nan_per)}")
print(f"  (expected ~50 per 5000-bar window = ~{len(df)//5000 * 50})")
print()

# Sample at NON-window-boundary positions
sample_idx = [2_500, 50_000, 150_000, 250_000, 350_000, 450_000, len(df)-1]
print("Sample rows at non-boundary positions:")
for i in sample_idx:
    if i < len(df):
        row = df.iloc[i]
        ema9 = f"{row['ema9']:.4f}" if pd.notna(row['ema9']) else "NaN"
        ema21 = f"{row['ema21']:.4f}" if pd.notna(row['ema21']) else "NaN"
        adx = f"{row['adx']:.2f}" if pd.notna(row['adx']) else "NaN"
        atr = f"{row['atr']:.4f}" if pd.notna(row['atr']) else "NaN"
        print(f"  [{i:>6}] {row.name}  close={row['close']:>8.2f}  ema9={ema9:>10}  ema21={ema21:>10}  adx={adx:>6}  atr={atr}")

# ---------------------------------------------------------------------------
# 2. Trade inspection (utc=True for mixed-tz handling)
# ---------------------------------------------------------------------------

print()
print("="*70)
print("2. TRADE INSPECTION")
print("="*70)

reports = sorted(Path("reports").glob("trades_*.csv"))
trade_file = reports[-1]
print(f"Reading: {trade_file}")

trades = pd.read_csv(trade_file)
trades["entry_time"] = pd.to_datetime(trades["entry_time"], utc=True, errors="coerce")
trades["exit_time"]  = pd.to_datetime(trades["exit_time"],  utc=True, errors="coerce")
trades["duration_min"] = (trades["exit_time"] - trades["entry_time"]).dt.total_seconds() / 60

print(f"Total trades: {len(trades):,}")
print(f"Date range: {trades['entry_time'].min()} -> {trades['exit_time'].max()}")
print()

print("Trade duration distribution:")
print(f"  min:    {trades['duration_min'].min():.1f} min")
print(f"  p10:    {trades['duration_min'].quantile(0.10):.1f} min")
print(f"  p25:    {trades['duration_min'].quantile(0.25):.1f} min")
print(f"  median: {trades['duration_min'].median():.1f} min")
print(f"  p75:    {trades['duration_min'].quantile(0.75):.1f} min")
print(f"  p90:    {trades['duration_min'].quantile(0.90):.1f} min")
print(f"  max:    {trades['duration_min'].max():.1f} min")
print()

quick = trades[trades["duration_min"] < 5]
print(f"Trades exiting within 5 minutes: {len(quick):,}  ({100*len(quick)/len(trades):.1f}%)")
zero = trades[trades["duration_min"] <= 0]
print(f"Trades with duration <= 0:      {len(zero):,}")
print()

if len(zero) > 0:
    print("Examples of zero-duration trades (THIS IS THE BUG IF NON-EMPTY):")
    print(zero.head(10)[["trade_id","entry_time","exit_time","entry_price",
                          "exit_price","exit_reason","pnl_dollars"]].to_string(index=False))
    print()

print("Exit reason vs. duration:")
for reason in trades["exit_reason"].unique():
    sub = trades[trades["exit_reason"] == reason]
    print(f"  {reason:>12}  n={len(sub):>5}  duration median={sub['duration_min'].median():>6.1f}m  pnl avg=${sub['pnl_dollars'].mean():>7.2f}")
print()

# ---------------------------------------------------------------------------
# 3. Year-by-year P&L
# ---------------------------------------------------------------------------

print("="*70)
print("3. YEAR-BY-YEAR PERFORMANCE")
print("="*70)

trades["year"] = trades["entry_time"].dt.year
yearly = trades.groupby("year").agg(
    n=("trade_id", "count"),
    pnl=("pnl_dollars", "sum"),
    win_rate=("pnl_dollars", lambda s: (s > 0).mean() * 100),
    avg=("pnl_dollars", "mean"),
).round(2)
print(yearly)
print()

print("Months with negative P&L (a real strategy should have many):")
trades["ym"] = trades["entry_time"].dt.to_period("M")
monthly = trades.groupby("ym")["pnl_dollars"].sum()
negative_months = monthly[monthly < 0]
print(f"  Total months: {len(monthly)}")
print(f"  Negative months: {len(negative_months)}  ({100*len(negative_months)/len(monthly):.1f}%)")
print(f"  Worst month: {monthly.min():.2f}  ({monthly.idxmin()})")
print(f"  Best month:  {monthly.max():.2f}  ({monthly.idxmax()})")
if len(negative_months) > 0 and len(negative_months) < 20:
    print()
    print("All negative months:")
    print(negative_months.to_string())

# ---------------------------------------------------------------------------
# 4. Big winner zoom
# ---------------------------------------------------------------------------

print()
print("="*70)
print("4. ZOOM ON ONE BIG WINNER")
print("="*70)

big = trades.nlargest(1, "pnl_dollars").iloc[0]
print(f"Trade #{int(big['trade_id'])}: {big['entry_time']} -> {big['exit_time']}")
print(f"  entry={big['entry_price']:.2f}  exit={big['exit_price']:.2f}  "
      f"pnl=${big['pnl_dollars']:.2f}  duration={big['duration_min']:.0f}m")