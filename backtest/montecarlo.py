"""
backtest/montecarlo.py — Escaflowne (Celeri-on-MGC)

Two analyses:

(1) Realistic-sizing analysis on walk 9 specifically
    Re-run the worst walk with use_scaling=True to confirm position sizing
    behavior matches what the fixed-1-contract test showed.

(2) Monte Carlo bootstrap on the optimized full-history backtest
    Resample the trade list 10,000 times to estimate the distribution of
    final P&L and max drawdown. The actual realized path is just one
    sample from this distribution. Bootstrap tells us what other paths
    were equally plausible.

Both use the optimized params from walk-forward:
  ADX = 10, EMA pair = (8, 21), ATR mult = 4.0, MIN_STOP_POINTS = 2.0

Run:
    python -m backtest.montecarlo
"""

from __future__ import annotations

import logging
from copy import copy
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Optimized params from Phase B walk-forward (most-frequent winner)
OPTIMIZED = {
    "adx_threshold":   10,
    "ema_fast":        8,
    "ema_slow":        21,
    "atr_stop_mult":   4.0,
    "min_stop_points": 2.0,
}


# ---------------------------------------------------------------------------
# Part 1 — Realistic-sizing rerun on walk 9 specifically
# ---------------------------------------------------------------------------

def realistic_sizing_walk9(starting_capital: float = 5_000.0):
    """Re-run walk 9 (the worst DD) with use_scaling=True."""
    from backtest.walkforward import _ConfigShim, ParamOverride, _recompute_5min, slice_window
    from backtest.engine import run_backtest
    from backtest.data_loader import load_mgc_1min, load_mgc, load_mgc_fast
    import config.backtest as cfg_template

    print("\n" + "="*70)
    print("PART 1 — REALISTIC SIZING ANALYSIS (walk 9 rerun)")
    print("="*70)
    print(f"Starting capital: ${starting_capital:,.0f}")
    print(f"Optimized params: {OPTIMIZED}")
    print()

    override = ParamOverride(**OPTIMIZED)
    cfg = _ConfigShim(cfg_template, override)

    df_1m = load_mgc_1min()
    df_5m_raw = load_mgc()

    # Walk 9 test window
    test_start = pd.Timestamp("2025-01-01")
    test_end   = pd.Timestamp("2025-07-01")

    # EMA pair is non-default (8 vs 9) — must recompute indicators on this slice
    df_5m_w = _recompute_5min(
        slice_window(df_5m_raw, test_start, test_end),
        ema_fast=8, ema_slow=21,
    )
    df_1m_w = slice_window(df_1m, test_start, test_end)

    print(f"Test window: {test_start.date()} -> {test_end.date()}")
    print(f"  1-min bars: {len(df_1m_w):,}   5-min bars: {len(df_5m_w):,}")

    # Run with realistic (risk-tiered) sizing
    results = run_backtest(
        df_1m=df_1m_w, df_5m=df_5m_w,
        starting_capital=starting_capital, use_scaling=True,
        instrument="MGC", cfg=cfg,
    )

    if not results.trades:
        print("No trades.")
        return

    df = pd.DataFrame([t.__dict__ for t in results.trades])
    eq = results.equity_curve
    dd_series = eq - eq.cummax()

    print(f"\nResults (use_scaling=True, MAX_CONTRACTS={cfg.MAX_CONTRACTS}):")
    print(f"  Trades:              {len(df):,}")
    print(f"  Total P&L:           ${df['pnl_dollars'].sum():,.2f}")
    print(f"  Final equity:        ${eq.iloc[-1]:,.2f}")
    print(f"  Max DD:              ${dd_series.min():,.2f}")
    print(f"  Max DD %:            {(dd_series.min() / eq.cummax().max() * 100):.2f}%")
    print(f"  Contracts per trade: {df['contracts'].value_counts().to_dict()}")

    # Compare to fixed-1-contract reference from walk-forward CSV
    wf_files = sorted((PROJECT_ROOT / "reports").glob("walkforward_phaseB_*.csv"))
    if wf_files:
        wf = pd.read_csv(wf_files[-1])
        w9 = wf[wf["walk"] == 9].iloc[0]
        print(f"\n  vs. fixed-1-contract walk 9: ${w9['pnl']:,.0f} pnl, ${w9['max_dd']:,.0f} DD")
        print(f"  Sizing impact: {'Same' if df['contracts'].max() == 1 else 'Different sizing'}")

    return results


# ---------------------------------------------------------------------------
# Part 2 — Monte Carlo bootstrap on full-history optimized backtest
# ---------------------------------------------------------------------------

def run_optimized_full_history(starting_capital: float = 5_000.0):
    """Run the full 7-year backtest with optimized params and use_scaling=True."""
    from backtest.walkforward import _ConfigShim, ParamOverride, _recompute_5min
    from backtest.engine import run_backtest
    from backtest.data_loader import load_mgc_1min, load_mgc
    import config.backtest as cfg_template

    print("\n" + "="*70)
    print("PART 2 — FULL-HISTORY BACKTEST WITH OPTIMIZED PARAMS")
    print("="*70)

    override = ParamOverride(**OPTIMIZED)
    cfg = _ConfigShim(cfg_template, override)

    df_1m = load_mgc_1min()
    df_5m_raw = load_mgc()

    # Recompute 5-min indicators with EMA(8, 21)
    print("Recomputing 5-min indicators with EMA(8, 21)...")
    df_5m = _recompute_5min(df_5m_raw, ema_fast=8, ema_slow=21)

    print(f"  1-min bars: {len(df_1m):,}   5-min bars: {len(df_5m):,}")

    results = run_backtest(
        df_1m=df_1m, df_5m=df_5m,
        starting_capital=starting_capital, use_scaling=True,
        instrument="MGC", cfg=cfg,
    )

    df = pd.DataFrame([t.__dict__ for t in results.trades])
    eq = results.equity_curve
    dd_series = eq - eq.cummax()

    print(f"\nFull-history optimized:")
    print(f"  Trades:        {len(df):,}")
    print(f"  Total P&L:     ${df['pnl_dollars'].sum():,.2f}")
    print(f"  Final equity:  ${eq.iloc[-1]:,.2f}")
    print(f"  Max DD:        ${dd_series.min():,.2f}  ({dd_series.min()/eq.cummax().max()*100:.2f}%)")
    print(f"  PF:            "
          f"{df.loc[df['is_winner'],'pnl_dollars'].sum() / abs(df.loc[~df['is_winner'],'pnl_dollars'].sum()):.2f}")
    print(f"  Win rate:      {(df['is_winner'].mean() * 100):.1f}%")

    return results


# ---------------------------------------------------------------------------
# Bootstrap MC
# ---------------------------------------------------------------------------

def monte_carlo_bootstrap(
    trades_df: pd.DataFrame,
    starting_capital: float = 5_000.0,
    n_sims: int = 10_000,
    seed: int = 42,
) -> dict:
    """
    Bootstrap-resample the trade list. For each simulation, sample N trades
    (with replacement) from the actual trade pool, compute the resulting
    equity curve and its drawdown.

    This estimates the distribution of paths a trader could plausibly have
    experienced trading this strategy during this regime.
    """
    print("\n" + "="*70)
    print(f"PART 3 — MONTE CARLO BOOTSTRAP ({n_sims:,} simulations)")
    print("="*70)

    rng = np.random.default_rng(seed)
    pnls = trades_df["pnl_dollars"].values
    n_trades = len(pnls)

    final_eq = np.empty(n_sims)
    max_dd   = np.empty(n_sims)
    min_eq   = np.empty(n_sims)

    print(f"Bootstrapping {n_sims:,} paths of {n_trades:,} trades each...")

    for i in range(n_sims):
        idx = rng.integers(0, n_trades, size=n_trades)
        sample_pnls = pnls[idx]
        equity = starting_capital + np.cumsum(sample_pnls)
        running_peak = np.maximum.accumulate(equity)
        dd = equity - running_peak

        final_eq[i] = equity[-1]
        max_dd[i]   = dd.min()
        min_eq[i]   = equity.min()

    # Stats
    print(f"\nFinal equity distribution:")
    print(f"  Mean:    ${final_eq.mean():>12,.0f}")
    print(f"  Median:  ${np.median(final_eq):>12,.0f}")
    print(f"  p5:      ${np.percentile(final_eq,  5):>12,.0f}")
    print(f"  p25:     ${np.percentile(final_eq, 25):>12,.0f}")
    print(f"  p75:     ${np.percentile(final_eq, 75):>12,.0f}")
    print(f"  p95:     ${np.percentile(final_eq, 95):>12,.0f}")
    print(f"  Min:     ${final_eq.min():>12,.0f}")
    print(f"  Max:     ${final_eq.max():>12,.0f}")

    print(f"\nMax drawdown distribution:")
    print(f"  Mean:    ${max_dd.mean():>12,.0f}  ({max_dd.mean()/starting_capital*100:>6.1f}% of starting)")
    print(f"  Median:  ${np.median(max_dd):>12,.0f}  ({np.median(max_dd)/starting_capital*100:>6.1f}% of starting)")
    print(f"  p5:      ${np.percentile(max_dd,  5):>12,.0f}  (worst 5%)")
    print(f"  p1:      ${np.percentile(max_dd,  1):>12,.0f}  (worst 1%)")
    print(f"  Worst:   ${max_dd.min():>12,.0f}")

    print(f"\nLowest equity touched (vs starting ${starting_capital:,.0f}):")
    print(f"  Mean:    ${min_eq.mean():>12,.0f}")
    print(f"  p5:      ${np.percentile(min_eq, 5):>12,.0f}")
    print(f"  p1:      ${np.percentile(min_eq, 1):>12,.0f}")
    print(f"  Min:     ${min_eq.min():>12,.0f}")

    print(f"\nProbability of important outcomes:")
    print(f"  P(profit > 0):              {(final_eq > starting_capital).mean()*100:.1f}%")
    print(f"  P(profit > 50% of start):   {(final_eq > 1.5*starting_capital).mean()*100:.1f}%")
    print(f"  P(profit > 2x start):       {(final_eq > 2*starting_capital).mean()*100:.1f}%")
    print(f"  P(any DD > 20% of start):   {(max_dd < -0.2*starting_capital).mean()*100:.1f}%")
    print(f"  P(any DD > 30% of start):   {(max_dd < -0.3*starting_capital).mean()*100:.1f}%")
    print(f"  P(any DD > 50% of start):   {(max_dd < -0.5*starting_capital).mean()*100:.1f}%")
    print(f"  P(equity ever < 50% start): {(min_eq < 0.5*starting_capital).mean()*100:.1f}%")
    print(f"  P(account blowup < 0):      {(min_eq < 0).mean()*100:.1f}%")

    return {
        "final_eq": final_eq,
        "max_dd":   max_dd,
        "min_eq":   min_eq,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=5000.0)
    parser.add_argument("--sims", type=int, default=10_000)
    parser.add_argument("--skip-walk9", action="store_true")
    parser.add_argument("--use-existing", action="store_true",
                        help="Load trades from latest reports/trades_*.csv instead of re-running")
    args = parser.parse_args()

    # Part 1
    if not args.skip_walk9:
        realistic_sizing_walk9(starting_capital=args.capital)

    # Part 2 — full history with optimized params
    if args.use_existing:
        # Use whatever is in the most recent trade list (probably the original
        # default-params backtest). Less accurate to the optimized config but
        # faster.
        reports = sorted((PROJECT_ROOT / "reports").glob("trades_*.csv"))
        latest = reports[-1]
        print(f"\nLoading existing trades: {latest.name}")
        trades_df = pd.read_csv(latest)
    else:
        results = run_optimized_full_history(starting_capital=args.capital)
        trades_df = pd.DataFrame([t.__dict__ for t in results.trades])

    # Part 3 — Monte Carlo
    mc_results = monte_carlo_bootstrap(
        trades_df, starting_capital=args.capital, n_sims=args.sims,
    )

    # Save
    out_dir = PROJECT_ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    ts_str = pd.Timestamp.now().strftime("%Y%m%d_%H%M")

    mc_df = pd.DataFrame({
        "final_eq": mc_results["final_eq"],
        "max_dd":   mc_results["max_dd"],
        "min_eq":   mc_results["min_eq"],
    })
    out = out_dir / f"montecarlo_{ts_str}.csv"
    mc_df.to_csv(out, index=False)
    print(f"\nMonte Carlo paths saved -> {out}")