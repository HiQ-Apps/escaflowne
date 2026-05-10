"""
backtest/engine.py — Escaflowne (Celeri-on-MGC)

Cloned from Celeri's engine.py. Only the run_backtest() (single-instrument)
path is kept — the combined MES+MNQ engine doesn't apply here.

The engine logic is IDENTICAL to Celeri:
  - 1-min heartbeat for intrabar stop checks
  - 5-min bars drive signal generation and cross-down exit detection
  - Cross-down exit fills at NEXT 1-min bar's open (market order)
  - Stop fills at worst(bar_open, stop_price) - slippage
  - EOD per ENABLE_EOD_EXIT setting
  - Risk-tiered position sizing
"""

from dataclasses import dataclass
from typing import Literal
from collections import deque
import pandas as pd

from strategy.signals import generate_signal, SessionState, Signal
import config.backtest as _cfg


# ---------------------------------------------------------------------------
# Constants pulled from config
# ---------------------------------------------------------------------------

COMMISSION_PER_SIDE = _cfg.COMMISSION_PER_SIDE
SLIPPAGE_TICKS      = _cfg.SLIPPAGE_TICKS
TICK_SIZE           = _cfg.TICK_SIZE
POINT_VALUE         = _cfg.POINT_VALUE
STARTING_CAPITAL    = _cfg.STARTING_CAPITAL


def _get_contracts(cash: float, stop_points: float, point_value: float, cfg) -> int:
    """Risk-tiered position sizing. Capped at MAX_CONTRACTS."""
    if stop_points <= 0 or point_value <= 0:
        return 1
    risk_pct = 0.005
    for threshold, pct in sorted(cfg.RISK_TIERS, reverse=True):
        if cash >= threshold:
            risk_pct = pct
            break
    contracts = int(cash * risk_pct / (stop_points * point_value))
    return max(1, min(contracts, cfg.MAX_CONTRACTS))


def _get_daily_loss_limit(cash: float, cfg) -> float:
    return -(cash * cfg.DAILY_LOSS_PCT)


def _should_close_for_eod(ts: pd.Timestamp, cfg) -> bool:
    eod_mode = getattr(cfg, "ENABLE_EOD_EXIT", True)
    if eod_mode is True:
        return ts.hour == 18 and ts.minute == 0
    if eod_mode == "friday_only":
        return ts.weekday() == 4 and ts.hour == 16 and ts.minute == 59
    return False


# ---------------------------------------------------------------------------
# Data structures (IDENTICAL to Celeri)
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    trade_id:     int
    signal:       str
    timeframe:    str
    entry_time:   pd.Timestamp
    exit_time:    pd.Timestamp = None
    entry_price:  float = 0.0
    exit_price:   float = 0.0
    stop_price:   float = 0.0
    exit_reason:  str   = ""
    pnl_points:   float = 0.0
    pnl_dollars:  float = 0.0
    contracts:    int   = 1
    instrument:   str   = ""
    is_winner:    bool  = False
    mfe_r:        float = 0.0
    mae_r:        float = 0.0
    mfe_points:   float = 0.0
    mae_points:   float = 0.0


@dataclass
class Position:
    entry_price:  float
    stop_price:   float
    signal:       Signal
    entry_time:   pd.Timestamp
    trade_id:     int
    timeframe:    str
    contracts:    int   = 1
    instrument:   str   = ""
    point_value:  float = 10.0
    max_favorable: float = None
    max_adverse:   float = None
    pending_cross_exit: bool = False


@dataclass
class BacktestResults:
    trades:           list[Trade]
    equity_curve:     pd.Series
    starting_capital: float = STARTING_CAPITAL


# ---------------------------------------------------------------------------
# Helpers (IDENTICAL to Celeri)
# ---------------------------------------------------------------------------

def _update_excursion(position: Position, bar: pd.Series) -> None:
    high, low = bar["high"], bar["low"]
    if position.max_favorable is None or high > position.max_favorable:
        position.max_favorable = high
    if position.max_adverse is None or low < position.max_adverse:
        position.max_adverse = low


def _check_stop(position: Position, bar: pd.Series) -> bool:
    return bar["low"] <= position.stop_price


def _close_position(position: Position, bar: pd.Series, ts: pd.Timestamp,
                    reason: str, point_value: float = None) -> Trade:
    slippage = SLIPPAGE_TICKS * TICK_SIZE
    pv       = point_value if point_value is not None else position.point_value
    c        = position.contracts

    if reason == "STOP":
        worst = min(bar["open"], position.stop_price)
        exit_price = worst - slippage
    else:
        exit_price = bar["open"] - slippage

    pnl_points  = exit_price - position.entry_price
    pnl_dollars = pnl_points * pv * c - COMMISSION_PER_SIDE * 2 * c

    risk_points = abs(position.entry_price - position.stop_price)
    mfe_points  = (position.max_favorable - position.entry_price) if position.max_favorable else 0.0
    mae_points  = (position.entry_price - position.max_adverse)   if position.max_adverse   else 0.0
    mfe_points  = max(0.0, mfe_points)
    mae_points  = max(0.0, mae_points)
    mfe_r = mfe_points / risk_points if risk_points > 0 else 0.0
    mae_r = mae_points / risk_points if risk_points > 0 else 0.0

    return Trade(
        trade_id=position.trade_id,
        signal=position.signal.reason,
        timeframe=position.timeframe,
        entry_time=position.entry_time,
        exit_time=ts,
        entry_price=position.entry_price,
        exit_price=exit_price,
        stop_price=position.stop_price,
        exit_reason=reason,
        pnl_points=pnl_points,
        pnl_dollars=pnl_dollars,
        contracts=c,
        instrument=position.instrument,
        is_winner=pnl_dollars > 0,
        mfe_r=mfe_r,
        mae_r=mae_r,
        mfe_points=mfe_points,
        mae_points=mae_points,
    )


# ---------------------------------------------------------------------------
# Single-instrument backtest (IDENTICAL to Celeri's run_backtest)
# ---------------------------------------------------------------------------

def run_backtest(
    df_1m: pd.DataFrame,
    df_5m: pd.DataFrame,
    starting_capital: float = STARTING_CAPITAL,
    point_value: float = None,
    use_scaling: bool = True,
    instrument: str = "MGC",
    cfg=None,
) -> BacktestResults:
    cfg = cfg or _cfg
    pv  = point_value if point_value is not None else cfg.POINT_VALUE
    df_1m = df_1m.dropna().copy()
    df_5m = df_5m.dropna().copy()

    df_5m_idx      = df_5m.index
    trades         = []
    equity         = []
    cash           = starting_capital
    position       = None
    state          = SessionState()
    prev_1m        = None
    trade_counter  = 0
    pending_signal = None
    pending_tf     = ""
    recent_5m      = deque(maxlen=2)
    current_date   = None
    daily_pnl      = 0.0

    print(f"Running backtest on {instrument} {'with dynamic sizing' if use_scaling else 'fixed 1 contract'}...")
    print(f"  1-min bars: {len(df_1m):,}  |  5-min bars: {len(df_5m):,}")
    print(f"  Date range: {df_1m.index[0]} -> {df_1m.index[-1]}")

    for ts, bar_1m in df_1m.iterrows():

        if ts.date() != current_date:
            current_date = ts.date()
            daily_pnl    = 0.0

        if ts.hour == 18 and ts.minute == 0:
            state = SessionState()
            state.daily_loss_limit = _get_daily_loss_limit(cash, cfg)

        if position is not None and _should_close_for_eod(ts, cfg):
            _update_excursion(position, bar_1m)
            trade     = _close_position(position, bar_1m, ts, "EOD", pv)
            cash     += trade.pnl_dollars
            daily_pnl += trade.pnl_dollars
            trades.append(trade)
            position  = None

        state.session_bars += 1
        state.in_position   = position is not None
        state.daily_pnl     = daily_pnl

        if position is not None and position.pending_cross_exit:
            _update_excursion(position, bar_1m)
            trade     = _close_position(position, bar_1m, ts, "CROSS_EXIT", pv)
            cash     += trade.pnl_dollars
            daily_pnl += trade.pnl_dollars
            trades.append(trade)
            position  = None

        if pending_signal is not None and position is None:
            slippage    = SLIPPAGE_TICKS * TICK_SIZE
            fill        = bar_1m["open"] + slippage
            stop_points = abs(pending_signal.entry_price - pending_signal.stop_price)
            contracts   = _get_contracts(cash, stop_points, pv, cfg) if use_scaling else 1
            trade_counter += 1
            position = Position(
                entry_price=fill,
                stop_price=pending_signal.stop_price,
                signal=pending_signal,
                entry_time=ts,
                trade_id=trade_counter,
                timeframe=pending_tf,
                contracts=contracts,
                instrument=instrument,
                point_value=pv,
                max_favorable=fill,
                max_adverse=fill,
            )
            state.trades_taken += 1
            pending_signal = None
            pending_tf     = ""

        if position is not None:
            _update_excursion(position, bar_1m)
            if _check_stop(position, bar_1m):
                trade     = _close_position(position, bar_1m, ts, "STOP", pv)
                cash     += trade.pnl_dollars
                daily_pnl += trade.pnl_dollars
                trades.append(trade)
                position  = None

        if ts in df_5m_idx:
            recent_5m.append(df_5m.loc[ts])
            if position is not None and len(recent_5m) >= 2:
                p5, c5 = recent_5m[-2], recent_5m[-1]
                if p5["ema9"] >= p5["ema21"] and c5["ema9"] < c5["ema21"]:
                    position.pending_cross_exit = True

        if position is None and pending_signal is None and prev_1m is not None:
            allowed = getattr(cfg, "ALLOWED_HOURS", None)
            if (allowed is None or ts.hour in allowed) and len(recent_5m) >= 2:
                sig = generate_signal(recent_5m[-2], recent_5m[-1], state, cfg, instrument=instrument)
                if sig:
                    pending_signal = sig
                    pending_tf     = "5min"

        equity.append(cash)
        prev_1m = bar_1m

    if position is not None:
        _update_excursion(position, df_1m.iloc[-1])
        trade = _close_position(position, df_1m.iloc[-1], df_1m.index[-1], "EOD", pv)
        cash += trade.pnl_dollars
        trades.append(trade)

    print(f"Backtest complete. Trades: {len(trades)}")
    return BacktestResults(
        trades=trades,
        equity_curve=pd.Series(equity, index=df_1m.index[:len(equity)]),
        starting_capital=starting_capital,
    )


# ---------------------------------------------------------------------------
# Print results (slimmed-down version of Celeri's _print_composite)
# ---------------------------------------------------------------------------

def print_results(results: BacktestResults, label: str = "MGC") -> None:
    W = 55
    print(f"\n{'=' * W}")
    print(f"  ESCAFLOWNE — BACKTEST RESULTS ({label})")
    print(f"{'=' * W}")

    if not results.trades:
        print("  No trades.")
        return

    df       = pd.DataFrame([t.__dict__ for t in results.trades])
    winners  = df[df["is_winner"]]
    losers   = df[~df["is_winner"]]
    total    = len(df)
    win_rate = len(winners) / total * 100
    total_pnl= df["pnl_dollars"].sum()
    pf       = (winners["pnl_dollars"].sum() /
                abs(losers["pnl_dollars"].sum()) if len(losers) else 0)
    eq       = results.equity_curve
    max_dd   = (eq - eq.cummax()).min()
    final_eq = eq.iloc[-1]
    avg_win  = winners["pnl_dollars"].mean() if len(winners) else 0
    avg_loss = losers["pnl_dollars"].mean()  if len(losers)  else 0

    print(f"  Total trades:    {total:,}")
    print(f"  Win rate:        {win_rate:.1f}%")
    print(f"  Profit factor:   {pf:.3f}")
    print(f"  Total P&L:       ${total_pnl:,.2f}")
    print(f"  Avg win:         ${avg_win:.2f}")
    print(f"  Avg loss:        ${avg_loss:.2f}")
    print(f"  Max drawdown:    ${max_dd:,.2f}")
    print(f"  Final equity:    ${final_eq:,.2f}")

    print(f"\n  Exit reason breakdown:")
    by_exit = df.groupby("exit_reason").agg(
        count=("pnl_dollars", "count"),
        total_pnl=("pnl_dollars", "sum"),
        avg_pnl=("pnl_dollars", "mean"),
        win_rate=("is_winner", lambda s: s.mean() * 100),
    ).round(2)
    print(by_exit.to_string())

    # Extended stats via stats.py
    try:
        from backtest.stats import compute_stats, print_stats
        s = compute_stats(
            results.trades, results.equity_curve,
            starting_capital=results.starting_capital,
            point_value=POINT_VALUE,
        )
        print_stats(s, label=label)
    except Exception as e:
        print(f"  (Extended stats unavailable: {e})")

    # Save trade list
    from pathlib import Path
    from datetime import datetime as _dt
    Path("reports").mkdir(exist_ok=True)
    ts_str = _dt.now().strftime("%Y%m%d_%H%M")
    df["run_label"] = label
    col_order = ["run_label","trade_id","instrument","signal","timeframe",
                 "entry_time","exit_time","entry_price","exit_price",
                 "stop_price","exit_reason",
                 "pnl_points","pnl_dollars","contracts","is_winner",
                 "mfe_r","mae_r","mfe_points","mae_points"]
    df = df[[c for c in col_order if c in df.columns]]
    out = f"reports/trades_{ts_str}.csv"
    df.to_csv(out, index=False)
    print(f"\n  Trade list -> {out}  ({len(df):,} trades)\n")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from backtest.data_loader import load_mgc_fast, load_mgc_1min

    print("Escaflowne Backtest Engine (MGC)")
    print("─" * 40)
    scaling = input("Dynamic position sizing? [Y/N]: ").strip().upper() != "N"
    print("\nLoading data...")

    results = run_backtest(
        df_1m=load_mgc_1min(),
        df_5m=load_mgc_fast(),
        starting_capital=STARTING_CAPITAL,
        use_scaling=scaling,
    )
    print_results(results, label="MGC")