"""
run_trade.py — Escaflowne PAPER TRADING entry point.

Connects LiveRunner (the brain) to LiveFeed (the eyes), placing actual orders
on paper account when the EMA/ADX strategy fires.

This is the FIRST file that places orders. Everything before this was watch-mode.

Safety boundaries:
  - Defaults to paper (port 4002). --live requires interactive confirmation.
  - Runner does orphan-stop cancellation + unprotected-position check on init.
  - All scar tissue from Celeri preserved: cross-exit double-fire guard,
    position adoption, kill switches.
  - Insufficient-capital signals are skipped (NLV<$7500 → no trade).

Usage:
    .venv\\Scripts\\python.exe run_trade.py          # paper, port 4002
    .venv\\Scripts\\python.exe run_trade.py --live   # LIVE, port 4001, confirms

Ctrl+C exits cleanly. Open positions remain managed by IBKR-side stops.
"""

# ── sys.path safety FIRST (before project imports) ──────────────────────────
# Same protection as run.py — prevents the rogue celeri repo on system
# Python's sys.path from shadowing Escaflowne's config.live.
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from dotenv import load_dotenv
load_dotenv()

import argparse
import logging
import threading
import time
from datetime import datetime

import pytz

from ib_insync import util

from live.broker import connect, disconnect, get_mgc_contract
from live.feed import LiveFeed
from live.runner import LiveRunner
from live.alerts import alert_shutdown, alert_disconnect

import config.live as cfg


ET = pytz.timezone("US/Eastern")


# ─────────────────────────────────────────────────────────────────────────────
# Logging — stdout + file
# ─────────────────────────────────────────────────────────────────────────────

LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FORMAT  = "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"
LOG_PATH    = HERE / "escaflowne_trade.log"


class SafeFileHandler(logging.FileHandler):
    """Retry on Windows file-lock errors."""
    def emit(self, record):
        for attempt in range(3):
            try:
                super().emit(record)
                return
            except (PermissionError, OSError):
                if attempt < 2:
                    time.sleep(0.1)


def _setup_logging():
    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setFormatter(fmt)

    file_h = SafeFileHandler(LOG_PATH, mode="a", delay=True, encoding="utf-8")
    file_h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(stdout_h)
    root.addHandler(file_h)

    # Quiet noisy libraries
    logging.getLogger("ib_insync.client").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)
    logging.getLogger("ib_insync.ib").setLevel(logging.WARNING)
    logging.getLogger("eventkit").setLevel(logging.ERROR)


_setup_logging()
log = logging.getLogger("escaflowne.main")


# ─────────────────────────────────────────────────────────────────────────────
# Periodic NLV heartbeat — every 15 min, log account state for overnight review
# ─────────────────────────────────────────────────────────────────────────────

class NLVHeartbeat(threading.Thread):
    INTERVAL_S = 15 * 60   # 15 minutes — less frequent than watch mode since
                            # runner already publishes state.json after every bar

    def __init__(self, ib, runner, started):
        super().__init__(daemon=True, name="nlv_heartbeat")
        self.ib      = ib
        self.runner  = runner
        self.started = started
        self._stop   = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        # First beat after 60s, then every interval
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            try:
                self._beat()
            except Exception as e:
                log.warning(f"heartbeat: {e}")
            if self._stop.wait(self.INTERVAL_S):
                return

    def _beat(self):
        if not self.ib.isConnected():
            log.warning("HEARTBEAT — ib_not_connected")
            return
        try:
            usd = [v for v in self.runner._acct_values() if v.currency == "USD"]
            nlv = float(next((v.value for v in usd if v.tag == "NetLiquidation"), 0))
        except Exception:
            nlv = 0.0
        uptime_h = (datetime.now(ET) - self.started).total_seconds() / 3600.0
        log.info(
            f"HEARTBEAT  up={uptime_h:.2f}h "
            f"nlv=${nlv:,.2f} "
            f"trades_today={self.runner.state.trades_taken} "
            f"daily_pnl=${self.runner._daily_pnl:+.2f} "
            f"in_position={self.runner.state.in_position} "
            f"killed={self.runner._killed} "
            f"status={self.runner.get_status()}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", default=False,
                        help="Connect to LIVE port 4001 (places REAL orders). "
                             "Requires interactive confirmation.")
    args  = parser.parse_args()
    paper = not args.live

    if not paper:
        print()
        print("  ⚠️  ⚠️  ⚠️   LIVE TRADING REQUESTED   ⚠️  ⚠️  ⚠️")
        print()
        print("  This will place REAL ORDERS on REAL MONEY on the Escaflowne")
        print("  live account. Orders fill on COMEX. Stops are STP LMT on IBKR.")
        print()
        confirm = input("  Type 'YES I UNDERSTAND' to proceed (anything else aborts): ")
        if confirm.strip() != "YES I UNDERSTAND":
            print("  Aborted.")
            sys.exit(0)

    started = datetime.now(ET)

    log.info("=" * 78)
    log.info(f"  Escaflowne TRADING MODE — {'PAPER' if paper else 'LIVE'}")
    log.info(f"  Started: {started.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info(
        f"  Params: EMA({cfg.EMA_FAST},{cfg.EMA_SLOW}) "
        f"ADX>={cfg.ADX_TREND_THRESHOLD} ADX_rising={cfg.ADX_MUST_RISE} "
        f"ATRx{cfg.ATR_STOP_MULT} min_stop={cfg.MIN_STOP_POINTS_MGC}pts"
    )
    log.info(f"  Log file: {LOG_PATH}")
    log.info("=" * 78)

    ib = None
    feed = None
    heartbeat = None
    runner = None

    try:
        # ── Connect ──────────────────────────────────────────────────────────
        ib = connect(paper=paper)
        contract = get_mgc_contract(ib)
        log.info(
            f"Contract qualified: {contract.localSymbol} "
            f"expiry={contract.lastTradeDateOrContractMonth} "
            f"conId={contract.conId}"
        )

        # ── Instantiate runner ───────────────────────────────────────────────
        # Runner __init__ does:
        #   - logs config
        #   - calls _cancel_orphans() (cancels any leftover protective orders)
        #   - calls _check_unprotected_positions() (emergency-flatten any naked positions)
        #   - sends "BOT STARTED" Discord alert
        runner = LiveRunner(ib=ib, contract=contract, paper=paper)
        log.info("Runner initialized — startup checks complete.")

        # ── Wire feed to runner.on_bar ───────────────────────────────────────
        # NOTE: feed expects a callback signature (prev, curr, label). Our
        # runner.on_bar matches. The label parameter is "MGC" — runner ignores
        # it since this bot is single-instrument.
        feed = LiveFeed(
            ib=ib,
            contract=contract,
            on_bar=runner.on_bar,
            label="MGC",
            get_status=runner.get_status,
        )
        feed.start()
        log.info("Feed subscribed — strategy is LIVE on completed 5-min bars.")

        # ── Heartbeat ────────────────────────────────────────────────────────
        heartbeat = NLVHeartbeat(ib, runner, started)
        heartbeat.start()

        # ── Disconnect handler ───────────────────────────────────────────────
        def _on_disconnect():
            log.error("IBKR CONNECTION LOST")
            try:
                alert_disconnect(paper)
            except Exception:
                pass
        ib.disconnectedEvent += _on_disconnect

        log.info("")
        log.info("Trading mode active. Press Ctrl+C to stop.")
        log.info("")

        try:
            util.run()
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received.")

    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    except ConnectionRefusedError:
        port = cfg.PORT_PAPER if paper else cfg.PORT_LIVE
        log.error(f"Cannot connect to Gateway on port {port}. Is it running?")
        sys.exit(1)
    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
    finally:
        log.info("Stopping...")
        if heartbeat is not None:
            heartbeat.stop()
        if feed is not None:
            try: feed.stop()
            except Exception: pass

        # IMPORTANT: do NOT cancel orders or flatten positions on shutdown.
        # If a position is open with an active STP LMT stop, that stop is
        # GTC and remains on the IBKR server when the bot exits. The
        # position is still protected. On restart, _adopt_position will
        # pick it up. This matches Celeri's behavior.

        if ib is not None and ib.isConnected():
            try: disconnect(ib)
            except Exception: pass

        try:
            alert_shutdown(paper, reason="trading mode stopped")
        except Exception:
            pass

        if runner is not None:
            log.info(
                f"FINAL trades_today={runner.state.trades_taken} "
                f"daily_pnl=${runner._daily_pnl:+.2f} "
                f"wins={runner._daily_wins} losses={runner._daily_losses} "
                f"killed={runner._killed}"
            )
        log.info("Escaflowne stopped.")


if __name__ == "__main__":
    main()