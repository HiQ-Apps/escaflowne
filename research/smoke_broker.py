"""
research/smoke_broker.py — Escaflowne smoke test for contracts.py + broker.py

Run this with the IB paper Gateway running on port 4002 to verify:
  1. broker.connect() can reach the paper Gateway
  2. contracts.front_month_yyyymm() produces a valid expiry
  3. broker.get_mgc_contract() qualifies the contract via IBKR
  4. We can fetch market data on the contract
  5. Account info is readable
  6. broker.disconnect() cleans up

Does NOT place any orders. Read-only.

Usage:
    cd /path/to/escaflowne
    python -m research.smoke_broker
"""

from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("smoke")

# ── Sanity: confirm imports work ─────────────────────────────────────────────
try:
    from live.contracts import (
        front_month_yyyymm,
        front_month_yyyymmdd,
        SCHEDULES,
    )
    from live.broker import (
        connect,
        disconnect,
        get_mgc_contract,
        has_open_position,
        get_position_size,
    )
except ImportError as e:
    log.error(f"Import failed — are you running from project root? Error: {e}")
    sys.exit(1)


def divider(title: str = ""):
    bar = "─" * 70
    if title:
        print(f"\n{bar}\n  {title}\n{bar}")
    else:
        print(bar)


def step1_contracts_local():
    """Verify contracts.py works in isolation, no IBKR needed."""
    divider("STEP 1 — contracts.py (local logic, no IBKR)")
    yyyymm   = front_month_yyyymm("MGC")
    yyyymmdd = front_month_yyyymmdd("MGC")
    log.info(f"Front-month YYYYMM:   {yyyymm}")
    log.info(f"Front-month YYYYMMDD: {yyyymmdd}")
    log.info(f"Schedule for MGC:     {SCHEDULES['MGC']}")
    if not (yyyymm.startswith("20") and len(yyyymm) == 6):
        log.error(f"  ✘ YYYYMM looks wrong: {yyyymm}")
        return False
    log.info("  ✓ contracts.py looks healthy")
    return True


def step2_connect():
    """Connect to paper Gateway on 4002."""
    divider("STEP 2 — connect to paper Gateway (port 4002)")
    try:
        ib = connect(paper=True)
        log.info(f"  ✓ Connected. ServerVersion={ib.client.serverVersion()}")
        return ib
    except ConnectionRefusedError:
        log.error("  ✘ Connection refused — is IB Gateway running on port 4002?")
        log.error("    Make sure you have a SECOND Gateway instance running")
        log.error("    logged in with PAPER credentials, port set to 4002.")
        return None
    except Exception as e:
        log.error(f"  ✘ Connection failed: {e}")
        return None


def step3_qualify_contract(ib):
    """Ask broker.py to qualify the MGC front month."""
    divider("STEP 3 — qualify MGC front month via IBKR")
    try:
        c = get_mgc_contract(ib)
        log.info(f"  ✓ Qualified")
        log.info(f"    symbol      = {c.symbol}")
        log.info(f"    localSymbol = {c.localSymbol}")
        log.info(f"    exchange    = {c.exchange}")
        log.info(f"    expiry      = {c.lastTradeDateOrContractMonth}")
        log.info(f"    conId       = {c.conId}")
        log.info(f"    multiplier  = {c.multiplier}")
        return c
    except Exception as e:
        log.error(f"  ✘ Qualification failed: {e}")
        return None


def step4_account_info(ib):
    """Read account info — confirms we're connected to the right account."""
    divider("STEP 4 — read paper account info")
    try:
        # Wait briefly for IB to populate accountValues
        ib.sleep(1.5)
        accounts = ib.managedAccounts()
        log.info(f"  Managed accounts: {accounts}")

        usd_values = [v for v in ib.accountValues() if v.currency == "USD"]
        nlv = next((float(v.value) for v in usd_values if v.tag == "NetLiquidation"), None)
        avail = next((float(v.value) for v in usd_values if v.tag == "AvailableFunds"), None)
        margin = next((float(v.value) for v in usd_values if v.tag == "MaintMarginReq"), None)

        log.info(f"  NetLiquidation:     ${nlv:,.2f}" if nlv is not None else "  NLV: <missing>")
        log.info(f"  AvailableFunds:     ${avail:,.2f}" if avail is not None else "  Available: <missing>")
        log.info(f"  Maint margin req:   ${margin:,.2f}" if margin is not None else "  MaintMargin: <missing>")
        log.info("  ✓ Account info readable")
        return True
    except Exception as e:
        log.error(f"  ✘ Account info failed: {e}")
        return False


def step5_market_data(ib, contract):
    """Subscribe to MGC ticker, wait for one update."""
    divider("STEP 5 — subscribe to live MGC market data")
    try:
        log.info("  Requesting market data...")
        ticker = ib.reqMktData(contract, "", snapshot=False, regulatorySnapshot=False)

        # Wait up to 8 seconds for at least one update
        for i in range(16):
            ib.sleep(0.5)
            if ticker.last is not None or ticker.bid is not None:
                break

        log.info(f"  bid={ticker.bid}  ask={ticker.ask}  last={ticker.last}")

        if ticker.last is None and ticker.bid is None:
            log.warning("  ⚠ No data received. Possible causes:")
            log.warning("    - Market is closed and no last trade is cached")
            log.warning("    - CME data subscription not active on this account")
            log.warning("    - Paper account doesn't have live data privileges")
            log.warning("  This isn't fatal for the smoke test, but worth checking")
        else:
            log.info("  ✓ Market data received")

        ib.cancelMktData(contract)
        return True
    except Exception as e:
        log.error(f"  ✘ Market data failed: {e}")
        return False


def step6_position_check(ib, contract):
    """Check current positions — should be flat in fresh paper account."""
    divider("STEP 6 — check current positions on the contract")
    try:
        size = get_position_size(ib, contract)
        has_pos = has_open_position(ib, contract)
        log.info(f"  Position size:  {size}")
        log.info(f"  Has position:   {has_pos}")
        if size == 0:
            log.info("  ✓ Account is flat (expected for fresh paper account)")
        else:
            log.warning(f"  ⚠ Existing position of {size} contracts — adopt logic will need to handle this when bot starts")
        return True
    except Exception as e:
        log.error(f"  ✘ Position check failed: {e}")
        return False


def main():
    print()
    log.info("=" * 70)
    log.info("ESCAFLOWNE SMOKE TEST — contracts.py + broker.py")
    log.info("=" * 70)
    log.info("Requires: IB Gateway running on port 4002 (paper trading)")
    print()

    # Step 1: local
    if not step1_contracts_local():
        log.error("ABORT — contracts.py is broken")
        sys.exit(1)

    # Step 2: connect
    ib = step2_connect()
    if ib is None:
        log.error("ABORT — could not connect to paper Gateway")
        sys.exit(1)

    success = True
    try:
        # Step 3: qualify
        c = step3_qualify_contract(ib)
        if c is None:
            success = False
        else:
            # Step 4: account
            success &= step4_account_info(ib)
            # Step 5: market data
            success &= step5_market_data(ib, c)
            # Step 6: position
            success &= step6_position_check(ib, c)
    finally:
        divider("CLEANUP — disconnect")
        try:
            disconnect(ib)
            log.info("  ✓ Disconnected cleanly")
        except Exception as e:
            log.warning(f"  Disconnect threw: {e}")

    print()
    if success:
        log.info("=" * 70)
        log.info("✓ ALL SMOKE TESTS PASSED")
        log.info("=" * 70)
        sys.exit(0)
    else:
        log.warning("=" * 70)
        log.warning("⚠ SOME TESTS FAILED — review output above")
        log.warning("=" * 70)
        sys.exit(2)


if __name__ == "__main__":
    main()