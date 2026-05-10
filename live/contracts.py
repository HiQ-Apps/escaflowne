"""
live/contracts.py — Escaflowne (Celeri-on-MGC)

Schedule-driven contract month resolution for COMEX gold (MGC).

Why this exists:
  The standard pattern in IBKR client code is to hardcode the contract expiry:
    contract = Future(symbol="MGC", lastTradeDateOrContractMonth="20260618")
  This requires editing the file every time the contract rolls (every 2 months
  for gold). Easy to forget; embarrassing when a "live" bot is silently trading
  an expired contract or refusing to trade because the front month is wrong.

  This file computes the front-month contract month from today's
  date based on COMEXvcontract schedule.

MGC contract specs:
  - Active months: Feb, Apr, Jun, Aug, Oct, Dec (every 2 months)
  - Last trading day: third-to-last business day of the contract month
  - Trading terminates at 13:30 ET on last trading day

Roll policy:
  Roll N business days before the last trading day of the current front
  month. The default (5 days) matches the bar exclusion in the backtest's
  data_loader._remove_roll_weeks. This keeps live behavior consistent with
  what the strategy was tested on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import pytz


ET = pytz.timezone("US/Eastern")


@dataclass(frozen=True)
class ContractSchedule:
    """Defines a futures contract's expiry rules."""
    symbol: str                       # e.g. "MGC"
    exchange: str                     # e.g. "COMEX"
    currency: str = "USD"
    active_months: tuple[int, ...] = (2, 4, 6, 8, 10, 12)   # MGC default
    # "third-to-last business day" → offset_from_end = 3 (1-indexed: 1 = last bday)
    last_trading_day_offset_from_end: int = 3


# Pre-defined schedules for common instruments
SCHEDULES = {
    "MGC": ContractSchedule(
        symbol="MGC",
        exchange="COMEX",
        active_months=(2, 4, 6, 8, 10, 12),
        last_trading_day_offset_from_end=3,
    ),
    # Future instruments could be added here. For example:
    # "MES": ContractSchedule(
    #     symbol="MES", exchange="CME",
    #     active_months=(3, 6, 9, 12),
    #     last_trading_day_offset_from_end=...,  # third Friday math is different
    # ),
}


def _last_trading_day(year: int, month: int, offset_from_end: int) -> pd.Timestamp:
    """
    Compute the Nth-from-last business day of (year, month).

    offset_from_end:
      1 = last business day
      2 = second-to-last business day
      3 = third-to-last business day  (MGC)

    Note: This does NOT account for US market holidays (Christmas, Thanksgiving,
    etc.) within the month. CME's actual last trading day can shift if the
    third-to-last business day falls on a holiday. For most months this is
    fine; for December specifically, double-check before going live.
    """
    if offset_from_end < 1:
        raise ValueError("offset_from_end must be >= 1")

    month_start = pd.Timestamp(year=year, month=month, day=1)
    if month == 12:
        next_month_start = pd.Timestamp(year=year + 1, month=1, day=1)
    else:
        next_month_start = pd.Timestamp(year=year, month=month + 1, day=1)

    bdays = pd.bdate_range(
        start=month_start,
        end=next_month_start - pd.Timedelta(days=1),
    )
    if len(bdays) < offset_from_end:
        # Degenerate case (shouldn't happen with real months)
        return bdays[-1]
    return bdays[-offset_from_end]


def front_month_for(
    schedule: ContractSchedule,
    asof: Optional[datetime] = None,
    roll_days_before: int = 5,
) -> tuple[int, int, pd.Timestamp]:
    """
    Determine the front-month (year, month) and its last-trading-day for the
    given contract schedule.

    Args:
        schedule:          contract schedule (e.g. SCHEDULES["MGC"])
        asof:              date to compute "front" against. Defaults to now in ET.
        roll_days_before:  roll to the next contract this many business days
                           BEFORE the current contract's last trading day.

    Returns:
        (year, month, last_trading_day) of the active front contract.

    Logic:
        1. Find the next active month that has a last_trading_day >= today
        2. If we're within `roll_days_before` of that day, jump to the
           subsequent active month
    """
    if asof is None:
        asof = datetime.now(ET)
    today = pd.Timestamp(asof).tz_localize(None).normalize()

    # Build list of (year, month, ltd) candidates spanning ~2 years
    candidates = []
    year = today.year
    for y_offset in range(-1, 3):
        cand_year = year + y_offset
        for m in schedule.active_months:
            ltd = _last_trading_day(cand_year, m, schedule.last_trading_day_offset_from_end)
            candidates.append((cand_year, m, ltd))

    # Sort by last trading day
    candidates.sort(key=lambda x: x[2])

    # Find first contract whose roll-trigger date is in the future
    # roll_trigger = ltd - roll_days_before business days
    for cand_year, cand_month, ltd in candidates:
        roll_trigger = ltd - pd.tseries.offsets.BDay(roll_days_before)
        if today < roll_trigger:
            return cand_year, cand_month, ltd

    # Fallback: use the latest candidate (shouldn't happen in practice)
    cand_year, cand_month, ltd = candidates[-1]
    return cand_year, cand_month, ltd


def front_month_yyyymm(
    symbol: str = "MGC",
    asof: Optional[datetime] = None,
    roll_days_before: int = 5,
) -> str:
    """
    Convenience: return the front-month YYYYMM string for IBKR's
    `lastTradeDateOrContractMonth` field. Wraps front_month_for().

    Example:
        >>> front_month_yyyymm("MGC")  # on 2026-05-09
        '202606'
    """
    schedule = SCHEDULES.get(symbol)
    if schedule is None:
        raise ValueError(f"No contract schedule defined for {symbol}")
    year, month, _ = front_month_for(schedule, asof=asof, roll_days_before=roll_days_before)
    return f"{year:04d}{month:02d}"


def front_month_yyyymmdd(
    symbol: str = "MGC",
    asof: Optional[datetime] = None,
    roll_days_before: int = 5,
) -> str:
    """
    Like front_month_yyyymm but returns the YYYYMMDD of the last trading day.
    IBKR accepts either format in lastTradeDateOrContractMonth.
    """
    schedule = SCHEDULES.get(symbol)
    if schedule is None:
        raise ValueError(f"No contract schedule defined for {symbol}")
    year, month, ltd = front_month_for(schedule, asof=asof, roll_days_before=roll_days_before)
    return f"{ltd.year:04d}{ltd.month:02d}{ltd.day:02d}"


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("MGC contract schedule:")
    print(f"  Active months: {SCHEDULES['MGC'].active_months}")
    print(f"  Last trading day offset: {SCHEDULES['MGC'].last_trading_day_offset_from_end}")
    print()

    print("Front month YYYYMM resolution across 2026:")
    for month in range(1, 13):
        for day in (1, 15, 25):
            asof = datetime(2026, month, day, 12, 0, tzinfo=ET)
            yyyymm = front_month_yyyymm("MGC", asof=asof)
            yyyymmdd = front_month_yyyymmdd("MGC", asof=asof)
            print(f"  asof {asof.date()} -> contract month {yyyymm}  (LTD {yyyymmdd})")