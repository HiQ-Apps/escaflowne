"""
research/verify.py — Sharpe verification + walk 9 DD inspection.

Three things:
  1. Recompute Sharpe a few different ways to find which one is honest
  2. Look at walk 9's intra-window drawdown — was -$1179 transient or sustained?
  3. Identify the optimized params for the Monte Carlo phase

Run after walk-forward Phase B has produced its CSV:
    python research/verify.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


def sharpe_per_trade(trades_df: pd.DataFrame, periods_per_year: int = 252) -> float:
    """
    Per-trade Sharpe: mean trade pnl / std trade pnl, scaled by sqrt(trades/year).
    No fragile daily resampling. Closer to what an institutional shop would report.
    """
    pnls = trades_df["pnl_dollars"]
    if pnls.std() == 0 or len(pnls) < 2:
        return 0.0
    mean = pnls.mean()
    sd   = pnls.std()
    # Annualize by trading frequency
    days_span = (pd.to_datetime(trades_df["entry_time"], utc=True).max()
                 - pd.to_datetime(trades_df["entry_time"], utc=True).min()).days
    if days_span <= 0:
        return 0.0
    trades_per_year = len(trades_df) * 365 / days_span
    return float((mean / sd) * math.sqrt(trades_per_year))


def sharpe_daily_dollars(trades_df: pd.DataFrame, periods_per_year: int = 252) -> float:
    """
    Sum P&L by calendar day, compute Sharpe on daily $ returns (not %).
    More appropriate for fixed-position-size futures than %-based Sharpe.
    """
    trades_df = trades_df.copy()
    trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"], utc=True)
    trades_df["date"] = trades_df["entry_time"].dt.date
    daily = trades_df.groupby("date")["pnl_dollars"].sum()
    if daily.std() == 0 or len(daily) < 2:
        return 0.0
    return float((daily.mean() / daily.std()) * math.sqrt(periods_per_year))


def sharpe_daily_pct(equity: pd.Series, periods_per_year: int = 252) -> float:
    """The original method from stats.py — for comparison."""
    eq_daily = equity.resample("1D").last().dropna()
    returns  = eq_daily.pct_change().dropna()
    if returns.std() == 0 or len(returns) < 2:
        return 0.0
    return float((returns.mean() / returns.std()) * math.sqrt(periods_per_year))


# ==========================================================================
# 1. SHARPE VERIFICATION on the original full-history backtest
# ==========================================================================

print("="*70)
print("1. SHARPE VERIFICATION — three different calculations")
print("="*70)

reports_dir = PROJECT_ROOT / "reports"
trade_files = sorted(reports_dir.glob("trades_*.csv"))
if not trade_files:
    print("No trades_*.csv found in reports/")
    sys.exit(1)

trade_file = trade_files[-1]   # most recent original backtest run
print(f"Using: {trade_file.name}\n")

trades = pd.read_csv(trade_file)
trades["entry_time"] = pd.to_datetime(trades["entry_time"], utc=True)
trades["exit_time"]  = pd.to_datetime(trades["exit_time"],  utc=True)

# Reconstruct equity curve from cumulative pnl (as if starting from $5K)
trades_sorted = trades.sort_values("exit_time")
equity = pd.Series(
    5000.0 + trades_sorted["pnl_dollars"].cumsum().values,
    index=trades_sorted["exit_time"],
)

s_pertrade = sharpe_per_trade(trades)
s_dollars  = sharpe_daily_dollars(trades)
s_pct      = sharpe_daily_pct(equity)

print(f"  Total trades:              {len(trades):,}")
print(f"  Total P&L:                 ${trades['pnl_dollars'].sum():,.2f}")
print(f"  Profit factor:             "
      f"{trades.loc[trades['is_winner'],'pnl_dollars'].sum() / abs(trades.loc[~trades['is_winner'],'pnl_dollars'].sum()):.2f}")
print()
print(f"  Sharpe (per-trade):        {s_pertrade:>6.2f}   <- most honest for fixed-size futures")
print(f"  Sharpe (daily $):          {s_dollars:>6.2f}   <- second-most honest")
print(f"  Sharpe (daily % via eq):   {s_pct:>6.2f}   <- the one stats.py uses (may be inflated)")
print()

# ==========================================================================
# 2. WALK 9 DD INSPECTION
# ==========================================================================

print()
print("="*70)
print("2. WALK 9 DRAWDOWN INSPECTION")
print("="*70)

# We don't have per-walk trade lists from walkforward.py, only the summary CSV.
# What we CAN do: rerun walk 9's test window on its best params and look at
# the drawdown evolution within that window.

wf_files = sorted(reports_dir.glob("walkforward_phaseB_*.csv"))
if not wf_files:
    print("No walk-forward Phase B CSV found.")
    sys.exit(0)

wf = pd.read_csv(wf_files[-1])
print(f"Using walk-forward file: {wf_files[-1].name}\n")
print(wf.to_string(index=False))
print()

walk9 = wf[wf["walk"] == 9].iloc[0]
print(f"\nWalk 9: {walk9['train_end']} -> {walk9['test_end']}, "
      f"params={walk9['best_params']}, pnl=${walk9['pnl']:.0f}, max_dd=${walk9['max_dd']:.0f}")

# Re-run walk 9's test to get its equity curve
print("\nRe-running walk 9 test window for DD inspection...")

from backtest.walkforward import _run_one, ParamOverride, _ConfigShim
from backtest.engine import run_backtest
from backtest.data_loader import load_mgc_1min, load_mgc, load_mgc_fast
import config.backtest as cfg_template

# Parse best_params back into a ParamOverride
# Format: "adx10_ema8-21_atr4.0_stop0.5"
parts = walk9["best_params"].split("_")
adx_v = int(parts[0].replace("adx", ""))
ema_parts = parts[1].replace("ema", "").split("-")
ef, es = int(ema_parts[0]), int(ema_parts[1])
atr_v = float(parts[2].replace("atr", ""))
stop_v = float(parts[3].replace("stop", ""))

override = ParamOverride(
    adx_threshold=adx_v, ema_fast=ef, ema_slow=es,
    atr_stop_mult=atr_v, min_stop_points=stop_v,
)

cfg = _ConfigShim(cfg_template, override)
df_1m = load_mgc_1min()
df_5m_ind = load_mgc_fast()  # default EMA pair

# Slice walk 9 test window
test_start = pd.Timestamp(walk9["train_end"]).tz_localize(df_1m.index.tz)
test_end   = pd.Timestamp(walk9["test_end"]).tz_localize(df_1m.index.tz)
df_1m_w = df_1m[(df_1m.index >= test_start) & (df_1m.index < test_end)]

# If best EMA pair is not (9,21) we'd need to recompute indicators on this slice.
# For the dominant case (ema8-21 etc.) we'd need recompute. To keep this script
# simple, we just run with whatever indicators were precomputed under default EMA.
# This is approximate but gives us the DD shape.
df_5m_w = df_5m_ind[(df_5m_ind.index >= test_start) & (df_5m_ind.index < test_end)]

print(f"  Test window has {len(df_1m_w):,} 1-min and {len(df_5m_w):,} 5-min bars")

if (override.ema_fast, override.ema_slow) != (9, 21):
    print(f"  NOTE: walk 9 used ema{override.ema_fast}-{override.ema_slow}, but we'll run with")
    print(f"        precomputed default ema9-21 indicators for DD shape inspection.")
    print(f"        Numbers won't match walk 9 exactly, but DD pattern should be representative.")

results = run_backtest(
    df_1m=df_1m_w, df_5m=df_5m_w,
    starting_capital=5000.0, use_scaling=False, instrument="MGC", cfg=cfg,
)

eq = results.equity_curve
dd_series = eq - eq.cummax()
print(f"\n  Re-run results:")
print(f"    Trades: {len(results.trades)}")
print(f"    Final equity: ${eq.iloc[-1]:,.2f}")
print(f"    Total P&L:    ${eq.iloc[-1] - 5000:,.2f}")
print(f"    Max DD:       ${dd_series.min():,.2f}")
print(f"    Max DD time:  {dd_series.idxmin()}")

# DD recovery analysis
peak = eq.cummax()
under_peak = eq < peak
in_dd_runs = []
in_dd = False
dd_start = None
for ts, below in under_peak.items():
    if below and not in_dd:
        dd_start = ts; in_dd = True
    elif not below and in_dd:
        in_dd_runs.append((dd_start, ts, (ts - dd_start).total_seconds() / 3600))
        in_dd = False
if in_dd:
    in_dd_runs.append((dd_start, eq.index[-1], (eq.index[-1] - dd_start).total_seconds() / 3600))

# Get DDs that exceeded $500
deep_dds = []
for start, end, hours in in_dd_runs:
    seg = eq[(eq.index >= start) & (eq.index <= end)]
    seg_peak = peak[(peak.index >= start) & (peak.index <= end)]
    deepest = (seg - seg_peak).min()
    if deepest < -200:
        deep_dds.append((start, end, hours, deepest))

print(f"\n  Drawdowns exceeding $200 within walk 9:")
for start, end, hours, deepest in sorted(deep_dds, key=lambda x: x[3])[:5]:
    print(f"    {start} -> {end}  ({hours:>5.1f}h)  worst: ${deepest:.0f}")


# ==========================================================================
# 3. OPTIMIZED PARAMS FOR MONTE CARLO PHASE
# ==========================================================================

print()
print("="*70)
print("3. PARAM SET FOR MONTE CARLO")
print("="*70)

print("\nBest-params frequency across walk-forward:")
counts = wf["best_params"].value_counts()
print(counts.to_string())

# Most-common param set
most_common = counts.idxmax()
print(f"\nMost frequent: {most_common}")
print(f"Picked in {counts.iloc[0]} of {len(wf)} walks ({100*counts.iloc[0]/len(wf):.0f}%)")
print(f"\nProposed Monte Carlo config — use these in config/backtest.py:")

parts = most_common.split("_")
print(f"  ADX_TREND_THRESHOLD = {int(parts[0].replace('adx',''))}")
print(f"  EMA_FAST            = {int(parts[1].replace('ema','').split('-')[0])}")
print(f"  EMA_SLOW            = {int(parts[1].replace('ema','').split('-')[1])}")
print(f"  ATR_STOP_MULT       = {float(parts[2].replace('atr',''))}")
print(f"  MIN_STOP_POINTS_MGC = {float(parts[3].replace('stop',''))}")