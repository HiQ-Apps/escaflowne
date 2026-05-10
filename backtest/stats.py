"""
backtest/stats.py — Escaflowne (Celeri-on-MGC)

VERBATIM COPY of Celeri's stats.py. Computes comprehensive backtest statistics:
  - Trade duration, quality, expectancy
  - Streaks, daily performance, equity curve stability
  - MFE/MAE excursion analysis
  - Performance breakdowns by signal, direction, day-of-week, hour
  - Scaling milestones
  - P&L distribution moments

Called automatically by engine.print_results.
Can also be called standalone:
    python -m backtest.stats reports/trades_YYYYMMDD_HHMM.csv
"""

import math
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_stats(
    trades: list,
    equity_curve: pd.Series,
    starting_capital: float = 5_000.0,
    point_value: float = 10.0,           # MGC default
    slippage_ticks: list[int] = None,
    tick_size: float = 0.10,             # MGC default
) -> dict:
    if not trades:
        return {}

    df = pd.DataFrame([t.__dict__ for t in trades])
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"]  = pd.to_datetime(df["exit_time"])

    winners = df[df["is_winner"]]
    losers  = df[~df["is_winner"]]

    stats = {}

    # Core counts
    stats["total_trades"]  = len(df)
    stats["total_winners"] = len(winners)
    stats["total_losers"]  = len(losers)
    stats["win_rate"]      = len(winners) / len(df) * 100

    gross_profit = winners["pnl_dollars"].sum() if len(winners) else 0
    gross_loss   = abs(losers["pnl_dollars"].sum()) if len(losers) else 1
    stats["profit_factor"] = gross_profit / gross_loss if gross_loss > 0 else 0
    stats["total_pnl"]     = df["pnl_dollars"].sum()
    stats["final_equity"]  = equity_curve.iloc[-1]

    # Trade duration
    df["duration_min"] = (df["exit_time"] - df["entry_time"]).dt.total_seconds() / 60
    stats["avg_duration_min"]    = df["duration_min"].mean()
    stats["median_duration_min"] = df["duration_min"].median()
    stats["min_duration_min"]    = df["duration_min"].min()
    stats["max_duration_min"]    = df["duration_min"].max()

    df["bars_held"] = (df["duration_min"] / 5).round().astype(int).clip(lower=1)
    stats["avg_bars_held"]    = df["bars_held"].mean()
    stats["median_bars_held"] = df["bars_held"].median()

    # Trade quality
    avg_win  = winners["pnl_dollars"].mean() if len(winners) else 0
    avg_loss = losers["pnl_dollars"].mean()  if len(losers)  else 0
    stats["avg_win"]        = avg_win
    stats["avg_loss"]       = avg_loss
    stats["win_loss_ratio"] = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    wr = stats["win_rate"] / 100
    lr = 1 - wr
    stats["expectancy_dollars"] = (wr * avg_win) + (lr * avg_loss)

    avg_contracts = df["contracts"].mean()
    stats["expectancy_points"] = (
        stats["expectancy_dollars"] / (avg_contracts * point_value)
        if avg_contracts > 0 else 0
    )

    df["stop_dist_pts"] = abs(df["entry_price"] - df["stop_price"])
    df["R_dollars"]     = df["stop_dist_pts"] * df["contracts"] * point_value
    df["pnl_in_R"]      = df.apply(
        lambda r: r["pnl_dollars"] / r["R_dollars"] if r["R_dollars"] > 0 else 0,
        axis=1
    )
    stats["expectancy_R"] = df["pnl_in_R"].mean()
    stats["avg_win_R"]    = df.loc[df["is_winner"],  "pnl_in_R"].mean() if len(winners) else 0
    stats["avg_loss_R"]   = df.loc[~df["is_winner"], "pnl_in_R"].mean() if len(losers)  else 0

    # Streaks
    streaks      = _compute_streaks(df["is_winner"].tolist())
    win_streaks  = [s for s in streaks if s > 0]
    loss_streaks = [abs(s) for s in streaks if s < 0]

    stats["max_win_streak"]  = max(win_streaks,  default=0)
    stats["max_loss_streak"] = max(loss_streaks, default=0)
    stats["avg_win_streak"]  = np.mean(win_streaks)  if win_streaks  else 0
    stats["avg_loss_streak"] = np.mean(loss_streaks) if loss_streaks else 0

    # Daily / session
    df["date"] = df["entry_time"].dt.date
    daily_pnl  = df.groupby("date")["pnl_dollars"].sum()

    stats["avg_daily_pnl"]    = daily_pnl.mean()
    stats["median_daily_pnl"] = daily_pnl.median()
    stats["best_day"]         = daily_pnl.max()
    stats["worst_day"]        = daily_pnl.min()
    stats["avg_trades_per_day"] = df.groupby("date").size().mean()
    stats["best_day_date"]    = daily_pnl.idxmax()
    stats["worst_day_date"]   = daily_pnl.idxmin()

    # Equity curve stability
    eq = equity_curve
    if isinstance(eq, pd.DataFrame):
        eq = eq.iloc[:, 0]

    eq_daily = eq.resample("1D").last().dropna()
    returns  = eq_daily.pct_change().dropna()

    stats["sharpe"]  = _sharpe(returns)
    stats["sortino"] = _sortino(returns)
    stats["ulcer"]   = _ulcer_index(eq_daily)

    dd_series = eq - eq.cummax()
    stats["max_drawdown"]      = dd_series.min()
    stats["max_drawdown_pct"]  = (dd_series.min() / eq.cummax().max()) * 100

    dd_dur, recovery = _drawdown_duration(eq)
    stats["longest_dd_duration_days"] = dd_dur
    stats["longest_recovery_days"]    = recovery

    # Context breakdowns
    if "instrument" in df.columns and df["instrument"].nunique() > 1:
        stats["by_instrument"] = _group_stats(df, "instrument")

    if "signal" in df.columns:
        stats["by_signal"] = _group_stats(df, "signal")

    if "signal" in df.columns:
        df["direction"] = df["signal"].apply(
            lambda s: "LONG" if "LONG" in str(s).upper() else "SHORT"
        )
        stats["by_direction"] = _group_stats(df, "direction")

    df["dow"] = df["entry_time"].dt.day_name()
    stats["by_dow"] = _group_stats(df, "dow")

    df["hour"] = df["entry_time"].dt.hour
    stats["by_hour"] = _group_stats(df, "hour")

    # Milestones
    milestones = [5_000, 10_000, 20_000, 50_000, 100_000]
    eq_vals    = equity_curve.values
    stats["milestones"] = {}
    for target in milestones:
        if eq_vals[-1] >= target:
            idx = np.argmax(eq_vals >= target)
            ts_at = equity_curve.index[idx]
            n_trades = (df["entry_time"] <= ts_at).sum()
            stats["milestones"][target] = int(n_trades)
        else:
            stats["milestones"][target] = None

    stats["by_contracts"] = _group_stats(df, "contracts")

    # Distribution
    stats["pnl_skew"]     = float(df["pnl_dollars"].skew())
    stats["pnl_kurtosis"] = float(df["pnl_dollars"].kurtosis())

    # MFE/MAE
    if "mfe_points" in df.columns and df["mfe_points"].notna().any():
        mfe = df["mfe_points"] * df["contracts"] * point_value
        mae = df["mae_points"] * df["contracts"] * point_value
        stats["avg_mfe"]            = mfe.mean()
        stats["avg_mae"]            = mae.mean()
        stats["profit_capture"]     = (
            abs(winners["pnl_dollars"].mean() / (mfe[df["is_winner"]].mean()))
            if mfe[df["is_winner"]].mean() > 0 else 0
        )
        stats["mfe_percentiles"] = {
            "p25": mfe.quantile(0.25), "p50": mfe.quantile(0.50),
            "p75": mfe.quantile(0.75), "p90": mfe.quantile(0.90),
        }
        stats["mae_percentiles"] = {
            "p25": mae.quantile(0.25), "p50": mae.quantile(0.50),
            "p75": mae.quantile(0.75), "p90": mae.quantile(0.90),
        }
    else:
        stats["mfe_available"] = False

    return stats


# ---------------------------------------------------------------------------
# Print
# ---------------------------------------------------------------------------

def print_stats(stats: dict, label: str = "") -> None:
    if not stats:
        print("  No stats available.")
        return

    W = 65
    hdr = f"  EXTENDED STATS{f' — {label}' if label else ''}"
    print(f"\n{'─' * W}")
    print(hdr)
    print(f"{'─' * W}")

    def row(name, val, fmt=""):
        if isinstance(val, float):
            if fmt == "$":   v = f"${val:,.2f}"
            elif fmt == "%": v = f"{val:.2f}%"
            elif fmt == "r": v = f"{val:.3f}R"
            elif fmt == "pts": v = f"{val:.2f} pts"
            elif fmt == "min":
                m = int(val); h = m // 60; rem = m % 60
                v = f"{h}h {rem}m" if h > 0 else f"{rem}m"
            else: v = f"{val:.3f}"
        elif isinstance(val, int): v = f"{val:,}"
        else: v = str(val)
        print(f"  {name:<40} {v:>20}")

    def section(title):
        print(f"\n  {'·' * 3} {title}")

    section("TRADE DURATION")
    row("Average duration",       stats["avg_duration_min"],    "min")
    row("Median duration",        stats["median_duration_min"], "min")
    row("Shortest trade",         stats["min_duration_min"],    "min")
    row("Longest trade",          stats["max_duration_min"],    "min")
    row("Avg bars held (5-min)",  stats["avg_bars_held"])
    row("Median bars held",       stats["median_bars_held"])

    section("TRADE QUALITY")
    row("Expectancy / trade",     stats["expectancy_dollars"],  "$")
    row("Expectancy in points",   stats["expectancy_points"],   "pts")
    row("Expectancy in R",        stats["expectancy_R"],        "r")
    row("Average win",            stats["avg_win"],             "$")
    row("Average loss",           stats["avg_loss"],            "$")
    row("Win / loss ratio",       stats["win_loss_ratio"])
    row("Avg win in R",           stats["avg_win_R"],           "r")
    row("Avg loss in R",          stats["avg_loss_R"],          "r")

    section("EXCURSION (MFE / MAE)")
    if stats.get("mfe_available") is False:
        print("  ⚠  MFE/MAE not tracked by engine yet.")
    else:
        row("Average MFE",              stats.get("avg_mfe", 0),     "$")
        row("Average MAE",              stats.get("avg_mae", 0),     "$")
        row("Profit capture ratio",     stats.get("profit_capture", 0))
        if "mfe_percentiles" in stats:
            p = stats["mfe_percentiles"]
            print(f"  {'MFE distribution (p25/p50/p75/p90)':<40} "
                  f"${p['p25']:.0f} / ${p['p50']:.0f} / ${p['p75']:.0f} / ${p['p90']:.0f}")
        if "mae_percentiles" in stats:
            p = stats["mae_percentiles"]
            print(f"  {'MAE distribution (p25/p50/p75/p90)':<40} "
                  f"${p['p25']:.0f} / ${p['p50']:.0f} / ${p['p75']:.0f} / ${p['p90']:.0f}")

    section("STREAKS")
    row("Max winning streak",     stats["max_win_streak"])
    row("Max losing streak",      stats["max_loss_streak"])
    row("Avg winning streak",     stats["avg_win_streak"])
    row("Avg losing streak",      stats["avg_loss_streak"])

    section("DAILY / SESSION PERFORMANCE")
    row("Average daily P&L",      stats["avg_daily_pnl"],    "$")
    row("Median daily P&L",       stats["median_daily_pnl"], "$")
    row(f"Best day  ({stats['best_day_date']})",
                                  stats["best_day"],         "$")
    row(f"Worst day ({stats['worst_day_date']})",
                                  stats["worst_day"],        "$")
    row("Avg trades per day",     stats["avg_trades_per_day"])

    section("EQUITY CURVE STABILITY")
    row("Sharpe ratio",           stats["sharpe"])
    row("Sortino ratio",          stats["sortino"])
    row("Ulcer index",            stats["ulcer"])
    row("Max drawdown",           stats["max_drawdown"],     "$")
    row("Max drawdown %",         stats["max_drawdown_pct"], "%")
    row("Longest DD duration",    float(stats["longest_dd_duration_days"]), "min")
    row("Longest recovery",       float(stats["longest_recovery_days"]),    "min")

    section("PERFORMANCE BY CONTEXT")
    for label_key, col_name in [
        ("By instrument",   "by_instrument"),
        ("By signal",       "by_signal"),
        ("By direction",    "by_direction"),
        ("By day of week",  "by_dow"),
    ]:
        if col_name in stats:
            _print_group(stats[col_name], label_key)

    if "by_hour" in stats:
        print(f"\n  By hour of day:")
        _print_group_table(stats["by_hour"], "Hour")

    section("SCALING MILESTONES")
    for target, n_trades in stats["milestones"].items():
        if n_trades is not None:
            print(f"  {'Reached $' + f'{target:,}':<40} {'after ' + f'{n_trades:,}' + ' trades':>20}")
        else:
            print(f"  {'Reached $' + f'{target:,}':<40} {'not reached':>20}")

    if "by_contracts" in stats:
        _print_group(stats["by_contracts"], "By contract size")

    section("DISTRIBUTION")
    row("P&L skew",       stats["pnl_skew"])
    row("P&L kurtosis",   stats["pnl_kurtosis"])
    note = ""
    if abs(stats["pnl_skew"]) < 0.5:
        note = "(fairly symmetric)"
    elif stats["pnl_skew"] > 0:
        note = "(right-skewed — occasional large winners)"
    else:
        note = "(left-skewed — occasional large losers ⚠)"
    print(f"  {'':40} {note:>20}")

    print(f"\n{'─' * W}\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_streaks(results: list[bool]) -> list[int]:
    if not results: return []
    streaks = []
    current = 1 if results[0] else -1
    for r in results[1:]:
        val = 1 if r else -1
        if val == (1 if current > 0 else -1):
            current += val
        else:
            streaks.append(current); current = val
    streaks.append(current)
    return streaks


def _sharpe(returns: pd.Series, periods_per_year: int = 252) -> float:
    if returns.std() == 0: return 0.0
    return float((returns.mean() / returns.std()) * math.sqrt(periods_per_year))


def _sortino(returns: pd.Series, periods_per_year: int = 252) -> float:
    downside = returns[returns < 0]
    if len(downside) == 0 or downside.std() == 0: return 0.0
    return float((returns.mean() / downside.std()) * math.sqrt(periods_per_year))


def _ulcer_index(equity: pd.Series) -> float:
    roll_max = equity.cummax()
    dd_pct   = ((equity - roll_max) / roll_max) * 100
    return float(math.sqrt((dd_pct ** 2).mean()))


def _drawdown_duration(equity: pd.Series):
    roll_max    = equity.cummax()
    in_dd       = equity < roll_max
    longest_dd  = 0
    longest_rec = 0
    dd_start    = None
    recovered   = True

    for ts, is_down in in_dd.items():
        if is_down and recovered:
            dd_start  = ts
            recovered = False
        elif not is_down and not recovered:
            dur = (ts - dd_start).days
            longest_dd  = max(longest_dd, dur)
            rec_dur = (ts - dd_start).days
            longest_rec = max(longest_rec, rec_dur)
            recovered   = True

    return longest_dd, longest_rec


def _group_stats(df: pd.DataFrame, col: str) -> dict:
    result = {}
    for val, grp in df.groupby(col):
        w = grp[grp["is_winner"]]
        l = grp[~grp["is_winner"]]
        gp = w["pnl_dollars"].sum() if len(w) else 0
        gl = abs(l["pnl_dollars"].sum()) if len(l) else 1
        result[val] = {
            "trades":  len(grp),
            "wr":      round(len(w) / len(grp) * 100, 1),
            "pf":      round(gp / gl if gl > 0 else 0, 2),
            "pnl":     round(grp["pnl_dollars"].sum(), 2),
            "avg_pnl": round(grp["pnl_dollars"].mean(), 2),
        }
    return result


def _print_group(group_dict: dict, title: str) -> None:
    if not group_dict: return
    print(f"\n  {title}:")
    print(f"  {'':20} {'Trades':>7} {'WR':>7} {'PF':>6} {'Total P&L':>14} {'Avg/trade':>11}")
    print(f"  {'':20} {'-'*7} {'-'*7} {'-'*6} {'-'*14} {'-'*11}")
    for key, s in sorted(group_dict.items(), key=lambda x: -x[1]["pnl"]):
        print(f"  {str(key):<20} {s['trades']:>7,} {s['wr']:>6.1f}% {s['pf']:>6.2f} "
              f"${s['pnl']:>13,.2f} ${s['avg_pnl']:>10,.2f}")


def _print_group_table(group_dict: dict, index_name: str) -> None:
    if not group_dict: return
    print(f"  {index_name:<8} {'Trades':>7} {'WR':>7} {'PF':>6} {'Avg P&L':>10}")
    for key in sorted(group_dict.keys()):
        s = group_dict[key]
        print(f"  {str(key):<8} {s['trades']:>7,} {s['wr']:>6.1f}% "
              f"{s['pf']:>6.2f} ${s['avg_pnl']:>9,.2f}")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m backtest.stats reports/trades_YYYYMMDD_HHMM.csv")
        sys.exit(1)

    path = sys.argv[1]
    print(f"Loading {path}...")
    df = pd.read_csv(path, parse_dates=["entry_time", "exit_time"])

    from types import SimpleNamespace
    trades = [SimpleNamespace(**row) for _, row in df.iterrows()]

    df = df.sort_values("entry_time")
    starting = 5_000.0
    equity = pd.Series(
        starting + df["pnl_dollars"].cumsum().values,
        index=df["exit_time"]
    )

    stats = compute_stats(trades, equity, starting_capital=starting, point_value=10.0)
    print_stats(stats, label=path)