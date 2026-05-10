"""
live/broker.py — Escaflowne (Celeri-on-MGC)

Handles IBKR connection, contract qualification, and helpers via ib_insync.

Key difference from Celeri's broker.py:
  Contract qualification is SCHEDULE-DRIVEN, not hardcoded. We compute the
  front-month contract month from today's date using live/contracts.py,
  then qualify it with IBKR. No more editing this file every time MGC rolls.

Ports:
  4002 — paper trading
  4001 — live trading
"""

from __future__ import annotations

import logging

from ib_insync import IB, Future

from live.contracts import (
    front_month_yyyymm,
    front_month_yyyymmdd,
    SCHEDULES,
)


log = logging.getLogger("escaflowne.broker")


# ─── Contract qualification ──────────────────────────────────────────────────

def get_mgc_contract(ib: IB, roll_days_before: int = 5) -> Future:
    """
    Returns a qualified front-month MGC contract.

    The contract month is computed dynamically from today's date — no
    hardcoded expiry strings. Rolls automatically `roll_days_before`
    business days ahead of the current contract's last trading day.

    Args:
        ib:                connected IB instance
        roll_days_before:  business days before LTD to roll forward (default 5,
                           matches the backtest's _remove_roll_weeks exclusion)

    Returns:
        Qualified Future contract for the active front-month MGC.
    """
    # YYYYMMDD is more specific than YYYYMM and avoids ambiguity in IBKR's
    # contract resolution. We use YYYYMMDD to lock onto the exact contract.
    expiry = front_month_yyyymmdd("MGC", roll_days_before=roll_days_before)
    yyyymm = front_month_yyyymm("MGC", roll_days_before=roll_days_before)

    contract = Future(
        symbol="MGC",
        exchange="COMEX",
        currency="USD",
        lastTradeDateOrContractMonth=expiry,
    )
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        # Fallback: try YYYYMM only, which IBKR resolves to its own LTD
        log.warning(
            f"Could not qualify MGC with LTD {expiry} — falling back to "
            f"contract month {yyyymm}"
        )
        contract = Future(
            symbol="MGC",
            exchange="COMEX",
            currency="USD",
            lastTradeDateOrContractMonth=yyyymm,
        )
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(
                f"Could not qualify MGC contract (tried LTD {expiry} and "
                f"month {yyyymm}). Check that IB Gateway is connected and "
                f"that MGC is supported on this account."
            )

    c = qualified[0]
    log.info(
        f"Qualified MGC contract: {c.localSymbol}  "
        f"expiry={c.lastTradeDateOrContractMonth}  conId={c.conId}"
    )
    return c


# ─── Connection ──────────────────────────────────────────────────────────────

def connect(paper: bool = True) -> IB:
    """
    Connect to IB Gateway. paper=True uses 4002, paper=False uses 4001.

    Tries clientId 10-19 to handle the IBKR daily-reset stale-session collision
    where Gateway holds onto the previous client session for several minutes
    after disconnect. Falls through to next clientId on error 326.

    NOTE on clientId range:
      Celeri uses clientIds 2-9. We use 10-19 for Escaflowne so the two bots
      don't compete for the same client slot if Celeri ever connects to the
      paper gateway (or if both connect to live with different account IDs).
    """
    port = 4002 if paper else 4001
    ib = IB()
    ib.RequestTimeout = 30

    last_error = None
    for client_id in range(10, 20):
        try:
            ib.connect("127.0.0.1", port, clientId=client_id, timeout=30)
            log.info(
                f"Connected to IBKR ({'PAPER' if paper else 'LIVE'}) "
                f"on port {port} clientId={client_id}"
            )
            return ib
        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            if "326" in err_str or "already in use" in err_str or "client id" in err_str:
                log.warning(f"clientId {client_id} in use, trying {client_id + 1}")
                continue
            raise

    raise ConnectionError(f"All clientIds 10-19 in use. Last error: {last_error}")


def disconnect(ib: IB):
    ib.disconnect()
    log.info("Disconnected from IBKR")


# ─── Order helpers ───────────────────────────────────────────────────────────

def cancel_all_orders(ib: IB):
    """Emergency cancel — wipes all open orders."""
    for trade in ib.openTrades():
        ib.cancelOrder(trade.order)
        log.warning(f"Cancelled order {trade.order.orderId}")


def has_open_position(ib: IB, contract: Future) -> bool:
    """Returns True if we currently hold any contracts of this product."""
    for pos in ib.positions():
        if pos.contract.conId == contract.conId and pos.position != 0:
            return True
    return False


def get_position_size(ib: IB, contract: Future) -> int:
    """Returns current net position in contracts (positive = long, negative = short)."""
    for pos in ib.positions():
        if pos.contract.conId == contract.conId:
            return int(pos.position)
    return 0