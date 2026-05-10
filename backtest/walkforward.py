"""
backtest/walkforward.py — Escaflowne (Celeri-on-MGC)

Two phases of out-of-sample validation:

Phase A — REGIME-CONDITIONAL (annual buckets, fixed Celeri params)
    Run the strategy with Celeri's exact parameters on each calendar year.
    Tells us: which regimes the edge works in, which it doesn't. No
    parameter search, no overfit risk — pure regime stability check.

Phase B — WALK-FORWARD WITH OPTIMIZATION
    For each walk: 24-month train, 6-month test, slide 6 months forward.
    In each train window, sweep 108 parameter combinations and pick the
    best Sharpe. Apply that to the test window. Concatenate all test
    windows into a "walked" equity curve.
    Tells us: how much in-sample Sharpe is overfitting vs real edge.

Decision criteria (committed before running):
    - Regime-conditional: 4+ of 8 years must show PF > 1.2, positive expectancy
    - Walk-forward: walked Sharpe must be >= 60% of in-sample Sharpe (3.41)
                    -> need walked Sharpe >= 2.0 to call this real
    - Worst single test window: max DD < 20% of starting capital
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import copy
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

import config.backtest as _cfg_template
from backtest.engine import run_backtest, BacktestResults
from backtest.stats import compute_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config-override helper
# ---------------------------------------------------------------------------

@dataclass
class ParamOverride:
    """A specific parameter combination to test, expressed as overrides
    to the base config."""
    adx_threshold:     int   = 14
    ema_fast:          int   = 9
    ema_slow:          int   = 21
    atr_stop_mult:     float = 3.0
    min_stop_points:   float = 1.0

    def label(self) -> str:
        return (f"adx{self.adx_threshold}_"
                f"ema{self.ema_fast}-{self.ema_slow}_"
                f"atr{self.atr_stop_mult}_"
                f"stop{self.min_stop_points}")


class _ConfigShim:
    """Mutable copy of config.backtest with overrides applied."""
    def __init__(self, base, override: ParamOverride):
        # Pull every attribute from base
        for name in dir(base):
            if not name.startswith("_"):
                setattr(self, name, getattr(base, name))
        # Apply overrides
        self.ADX_TREND_THRESHOLD = override.adx_threshold
        self.ATR_STOP_MULT       = override.atr_stop_mult
        self.MIN_STOP_POINTS_MGC = override.min_stop_points


def make_param_grid() -> list[ParamOverride]:
    """The committed grid. 108 combinations."""
    grid = []
    adx_values        = [10, 14, 18, 22]
    ema_pairs         = [(8, 21), (9, 21), (12, 26)]
    atr_mults         = [2.0, 3.0, 4.0]
    min_stop_values   = [0.5, 1.0, 2.0]

    for adx, (ef, es), atr_m, ms in product(adx_values, ema_pairs, atr_mults, min_stop_values):
        grid.append(ParamOverride(
            adx_threshold=adx,
            ema_fast=ef,
            ema_slow=es,
            atr_stop_mult=atr_m,
            min_stop_points=ms,
        ))
    return grid


# ---------------------------------------------------------------------------
# Per-window indicator recompute (needed when EMA periods change)
# ---------------------------------------------------------------------------

def _recompute_5min(df_5m_raw: pd.DataFrame, ema_fast: int, ema_slow: int) -> pd.DataFrame:
    """
    Recompute indicators with a different EMA pair. Used inside the parameter
    sweep — most params (ADX threshold, ATR mult, min stop) don't require recompute,
    but EMA pair changes do.

    The non-EMA indicators (ADX, ATR, volume_ma) are fixed at Celeri's standard
    periods and shared across all sweep iterations.
    """
    import pandas_ta as ta
    from strategy.indicators import INDICATOR_WINDOW, ADX_PERIOD, ATR_PERIOD, VOL_MA_PERIOD

    n = len(df_5m_raw)
    result = pd.DataFrame(index=df_5m_raw.index, columns=[
        "ema9", "ema21", "adx", "plus_di", "minus_di", "volume_ma", "atr",
    ], dtype=float)

    starts = list(range(0, n, INDICATOR_WINDOW))
    if starts[-1] + INDICATOR_WINDOW < n:
        starts.append(n - INDICATOR_WINDOW)

    for start in starts:
        end   = min(start + INDICATOR_WINDOW, n)
        chunk = df_5m_raw.iloc[start:end]

        ef = ta.ema(chunk["close"],  length=ema_fast)
        es = ta.ema(chunk["close"],  length=ema_slow)
        vm = ta.sma(chunk["volume"], length=VOL_MA_PERIOD)
        adx_df = ta.adx(chunk["high"], chunk["low"], chunk["close"], length=ADX_PERIOD)
        atr    = ta.atr(chunk["high"], chunk["low"], chunk["close"], length=ATR_PERIOD)

        warmup    = 50
        valid_idx = chunk.index[warmup:]

        # Note: signals.py reads "ema9" and "ema21" by name, regardless of the
        # actual periods. So we always populate those column names with whatever
        # fast/slow EMAs the param set specifies.
        result.loc[valid_idx, "ema9"]      = ef.loc[valid_idx].values
        result.loc[valid_idx, "ema21"]     = es.loc[valid_idx].values
        result.loc[valid_idx, "volume_ma"] = vm.loc[valid_idx].values
        result.loc[valid_idx, "adx"]       = adx_df[f"ADX_{ADX_PERIOD}"].loc[valid_idx].values
        result.loc[valid_idx, "plus_di"]   = adx_df[f"DMP_{ADX_PERIOD}"].loc[valid_idx].values
        result.loc[valid_idx, "minus_di"]  = adx_df[f"DMN_{ADX_PERIOD}"].loc[valid_idx].values
        result.loc[valid_idx, "atr"]       = atr.loc[valid_idx].values

    return pd.concat([df_5m_raw, result], axis=1)


# ---------------------------------------------------------------------------
# Slicing helpers
# ---------------------------------------------------------------------------

def slice_window(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Inclusive of start, exclusive of end. Both args tz-aware preferred."""
    if df.index.tz is not None:
        if start.tz is None:
            start = start.tz_localize(df.index.tz)
        if end.tz is None:
            end = end.tz_localize(df.index.tz)
    return df.loc[(df.index >= start) & (df.index < end)].copy()


# ---------------------------------------------------------------------------
# One-shot backtest with overrides + window
# ---------------------------------------------------------------------------

def _run_one(
    df_1m_raw: pd.DataFrame,
    df_5m_raw: pd.DataFrame,
    df_5m_ind_default: pd.DataFrame,  # precomputed with default EMA pair (9, 21)
    start: pd.Timestamp,
    end: pd.Timestamp,
    override: ParamOverride,
    starting_capital: float = 5_000.0,
    use_scaling: bool = True,
) -> dict:
    """
    Run a single backtest on [start, end] with the given parameter override.
    Returns a dict of summary metrics.
    """
    cfg = _ConfigShim(_cfg_template, override)

    # If EMA pair differs from default, recompute indicators on this slice
    needs_recompute = (override.ema_fast != 9) or (override.ema_slow != 21)
    if needs_recompute:
        df_5m = _recompute_5min(slice_window(df_5m_raw, start, end),
                                 override.ema_fast, override.ema_slow)
    else:
        df_5m = slice_window(df_5m_ind_default, start, end)

    df_1m = slice_window(df_1m_raw, start, end)

    if len(df_1m) == 0 or len(df_5m) == 0:
        return {"override": override, "valid": False}

    results = run_backtest(
        df_1m=df_1m,
        df_5m=df_5m,
        starting_capital=starting_capital,
        use_scaling=use_scaling,
        instrument="MGC",
        cfg=cfg,
    )

    if not results.trades:
        return {
            "override": override, "valid": True, "n_trades": 0,
            "pnl": 0, "sharpe": 0, "pf": 0, "max_dd": 0, "win_rate": 0,
            "final_eq": starting_capital,
        }

    stats = compute_stats(results.trades, results.equity_curve,
                          starting_capital=starting_capital, point_value=cfg.POINT_VALUE)

    return {
        "override":  override,
        "valid":     True,
        "n_trades":  stats["total_trades"],
        "pnl":       stats["total_pnl"],
        "sharpe":    stats["sharpe"],
        "sortino":   stats["sortino"],
        "pf":        stats["profit_factor"],
        "win_rate":  stats["win_rate"],
        "max_dd":    stats["max_drawdown"],
        "max_dd_pct": stats["max_drawdown_pct"],
        "final_eq":  stats["final_equity"],
        "expectancy_R": stats["expectancy_R"],
    }


# ---------------------------------------------------------------------------
# Phase A — annual buckets with fixed Celeri params
# ---------------------------------------------------------------------------

def phase_a_regime_conditional(
    df_1m_raw: pd.DataFrame,
    df_5m_ind: pd.DataFrame,
    starting_capital: float = 5_000.0,
) -> pd.DataFrame:
    """Run Celeri's exact params on each calendar year independently."""
    print("\n" + "="*70)
    print("PHASE A — REGIME-CONDITIONAL (annual buckets, fixed Celeri params)")
    print("="*70)

    default_override = ParamOverride()
    years = sorted(df_5m_ind.index.year.unique())
    rows = []

    for year in years:
        start = pd.Timestamp(f"{year}-01-01")
        end   = pd.Timestamp(f"{year+1}-01-01")
        result = _run_one(
            df_1m_raw=df_1m_raw,
            df_5m_raw=None,            # not needed when no recompute
            df_5m_ind_default=df_5m_ind,
            start=start, end=end,
            override=default_override,
            starting_capital=starting_capital,
            use_scaling=False,         # fixed 1 contract = pure edge measurement
        )
        rows.append({
            "year": year, **{k: v for k, v in result.items() if k != "override"},
        })

    df = pd.DataFrame(rows)
    print()
    print(df.to_string(index=False))

    # Verdict
    passed = (df["pf"] > 1.2) & (df["expectancy_R"] > 0)
    print(f"\nYears passing PF>1.2 and positive expectancy: {passed.sum()} of {len(df)}")
    print(f"Decision criterion: 4+ of 8 -> {'PASS' if passed.sum() >= 4 else 'FAIL'}")
    return df


# ---------------------------------------------------------------------------
# Phase B — walk-forward with optimization
# ---------------------------------------------------------------------------

def _run_one_starargs(args):
    return _run_one(*args)


def _train_and_pick_best(
    df_1m_raw, df_5m_raw, df_5m_ind_default,
    train_start, train_end,
    grid: list[ParamOverride],
    starting_capital: float,
    n_workers: int,
) -> ParamOverride:
    """Run all grid combos on the train window, pick the best by Sharpe."""
    args_iter = [
        (df_1m_raw, df_5m_raw, df_5m_ind_default, train_start, train_end,
         override, starting_capital, False)  # fixed 1 contract during search
        for override in grid
    ]

    results = []
    if n_workers > 1:
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            for r in ex.map(_run_one_starargs, args_iter, chunksize=4):
                results.append(r)
    else:
        for args in args_iter:
            results.append(_run_one_starargs(args))

    # Filter valid results with non-zero trades
    valid = [r for r in results if r.get("valid") and r.get("n_trades", 0) > 5]
    if not valid:
        # No combo produced trades; return the default
        return ParamOverride()

    # Best by Sharpe; tie-break on PF
    best = max(valid, key=lambda r: (r["sharpe"], r["pf"]))
    return best["override"]


def phase_b_walkforward(
    df_1m_raw: pd.DataFrame,
    df_5m_raw: pd.DataFrame,
    df_5m_ind_default: pd.DataFrame,
    train_months: int = 24,
    test_months: int = 6,
    step_months: int = 6,
    starting_capital: float = 5_000.0,
    n_workers: int = 4,
) -> pd.DataFrame:
    """
    Rolling walk-forward:
      walk N: train on [t, t+train_months), test on [t+train_months, t+train_months+test_months)
      slide forward step_months and repeat.
    """
    print("\n" + "="*70)
    print("PHASE B — WALK-FORWARD WITH OPTIMIZATION")
    print(f"  Grid:  {len(make_param_grid())} parameter combinations")
    print(f"  Train: {train_months} months   Test: {test_months} months   Step: {step_months} months")
    print(f"  Workers: {n_workers}")
    print("="*70)

    # Determine walk schedule
    data_start = df_5m_ind_default.index.min().tz_localize(None)
    data_end   = df_5m_ind_default.index.max().tz_localize(None)

    grid = make_param_grid()
    walks = []
    walk_start = data_start.normalize().replace(day=1)

    while True:
        train_end_dt = walk_start + pd.DateOffset(months=train_months)
        test_end_dt  = train_end_dt + pd.DateOffset(months=test_months)
        if test_end_dt > data_end:
            break
        walks.append((walk_start, train_end_dt, test_end_dt))
        walk_start += pd.DateOffset(months=step_months)

    print(f"\n  Walks scheduled: {len(walks)}")

    rows = []
    for i, (ts, te, tt) in enumerate(walks, 1):
        print(f"\n  --- Walk {i}/{len(walks)} ---")
        print(f"    Train: {ts.date()} -> {te.date()}")
        print(f"    Test:  {te.date()} -> {tt.date()}")

        best_override = _train_and_pick_best(
            df_1m_raw, df_5m_raw, df_5m_ind_default,
            ts, te, grid, starting_capital, n_workers=n_workers,
        )
        print(f"    Best train params: {best_override.label()}")

        # Now run test
        test_result = _run_one(
            df_1m_raw, df_5m_raw, df_5m_ind_default,
            te, tt, best_override, starting_capital, use_scaling=False,
        )
        print(f"    Test sharpe={test_result.get('sharpe', 0):.2f}  "
              f"PF={test_result.get('pf', 0):.2f}  "
              f"trades={test_result.get('n_trades', 0)}  "
              f"pnl=${test_result.get('pnl', 0):.0f}")

        rows.append({
            "walk":         i,
            "train_start":  ts.date(),
            "train_end":    te.date(),
            "test_end":     tt.date(),
            "best_params":  best_override.label(),
            **{k: v for k, v in test_result.items() if k not in ("override",)},
        })

    df = pd.DataFrame(rows)
    print("\n" + "─"*70)
    print("WALK-FORWARD SUMMARY")
    print("─"*70)
    cols = ["walk", "train_start", "train_end", "test_end", "best_params",
            "n_trades", "pnl", "sharpe", "pf", "win_rate", "max_dd"]
    print(df[[c for c in cols if c in df.columns]].to_string(index=False))

    # Aggregate metrics
    if len(df):
        avg_sharpe   = df["sharpe"].mean()
        median_sharpe = df["sharpe"].median()
        total_pnl    = df["pnl"].sum()
        worst_dd     = df["max_dd"].min()
        passing      = (df["sharpe"] > 0) & (df["pf"] > 1.0)
        print(f"\n  Walks with positive Sharpe + PF>1: {passing.sum()} of {len(df)}")
        print(f"  Mean walked Sharpe:    {avg_sharpe:.2f}")
        print(f"  Median walked Sharpe:  {median_sharpe:.2f}")
        print(f"  Aggregate test P&L:    ${total_pnl:,.2f}")
        print(f"  Worst single-walk DD:  ${worst_dd:.2f}")
        print()
        print(f"  Decision criteria:")
        print(f"    Walked Sharpe >= 2.0?    {'PASS' if avg_sharpe >= 2.0 else 'FAIL'} ({avg_sharpe:.2f})")
        print(f"    Worst DD < 20% capital?   {'PASS' if abs(worst_dd) < 0.2*starting_capital else 'FAIL'} (${worst_dd:.0f})")

    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["a", "b", "both"], default="both")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    from backtest.data_loader import load_mgc_1min, load_mgc, load_mgc_fast

    print("Loading data...")
    df_1m       = load_mgc_1min()
    df_5m_raw   = load_mgc()
    df_5m_ind   = load_mgc_fast()

    print(f"  1-min bars: {len(df_1m):,}")
    print(f"  5-min bars: {len(df_5m_ind):,}")
    print(f"  Date range: {df_5m_ind.index[0]} -> {df_5m_ind.index[-1]}")

    out = Path("reports")
    out.mkdir(exist_ok=True)
    ts_str = pd.Timestamp.now().strftime("%Y%m%d_%H%M")

    if args.phase in ("a", "both"):
        df_a = phase_a_regime_conditional(df_1m, df_5m_ind)
        df_a.to_csv(out / f"walkforward_phaseA_{ts_str}.csv", index=False)
        print(f"\nPhase A results -> reports/walkforward_phaseA_{ts_str}.csv")

    if args.phase in ("b", "both"):
        df_b = phase_b_walkforward(df_1m, df_5m_raw, df_5m_ind, n_workers=args.workers)
        df_b.to_csv(out / f"walkforward_phaseB_{ts_str}.csv", index=False)
        print(f"\nPhase B results -> reports/walkforward_phaseB_{ts_str}.csv")