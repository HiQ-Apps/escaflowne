"""
run.py — Escaflowne (WATCH MODE)

Stripped-down entry point that runs WITHOUT runner.py. Used for paper-trading
infrastructure validation before the runner is built. Connects to IBKR paper
Gateway, qualifies the MGC front-month contract, subscribes to 5-min bars, and
streams everything to console + log file + Discord (start/stop alerts only).

Once runner.py is ported, replace the `_on_bar_watch` callback with a real
LiveRunner.on_bar and add proper position management.

Usage:
    python run.py          # paper (default — port 4002)
    python run.py --live   # REAL MONEY (port 4001) — refuses without confirmation

Ctrl+C exits cleanly.
"""

from dotenv import load_dotenv
load_dotenv()

import argparse
import collections
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from ib_insync import util
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box
import pytz

from live.broker import connect, disconnect, get_mgc_contract
from live.feed import LiveFeed
from live.alerts import alert_shutdown, alert_disconnect, alert_restart
from live.state_publisher import write_state

import config.live as cfg


ET = pytz.timezone("US/Eastern")


# ─────────────────────────────────────────────────────────────────────────────
# Logging — file + buffer (no raw stdout, the dashboard handles display)
# ─────────────────────────────────────────────────────────────────────────────

LOG_DATEFMT  = "%Y-%m-%d %H:%M:%S"
_log_buffer  = collections.deque(maxlen=12)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            _log_buffer.append((record.levelno, msg))
        except Exception:
            pass


class SafeFileHandler(logging.FileHandler):
    """File handler that retries on Windows-style file-locked errors."""
    def emit(self, record):
        for attempt in range(3):
            try:
                super().emit(record)
                return
            except (PermissionError, OSError):
                if attempt < 2:
                    time.sleep(0.1)


_buf_handler  = _BufferHandler()
_buf_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt=LOG_DATEFMT))

_file_handler = SafeFileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "escaflowne_live.log"),
    mode="a", delay=True, encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt=LOG_DATEFMT))

logging.basicConfig(level=logging.INFO, handlers=[_buf_handler, _file_handler])
logging.getLogger("ib_insync").setLevel(logging.ERROR)
logging.getLogger("eventkit").setLevel(logging.ERROR)
log = logging.getLogger("escaflowne")


# ─────────────────────────────────────────────────────────────────────────────
# Watch-mode bar handler
# ─────────────────────────────────────────────────────────────────────────────
# This stands in for runner.on_bar. It only logs each completed bar's
# OHLCV and indicator values — no signal evaluation, no orders.

_bars_seen     = [0]            # mutable container so closures can update
_last_bar_time = [time.time()]  # for watchdog


def _on_bar_watch(prev, curr, label: str):
    """
    Called whenever a completed 5-min bar arrives. Just logs it.
    `prev` and `curr` are Pandas rows with OHLCV + ema9/ema21/adx/atr columns
    (see live/feed.py for the indicator schema).
    """
    _bars_seen[0]     += 1
    _last_bar_time[0]  = time.time()

    # Compute whether the prev->curr transition would have crossed up or down
    # so the operator can see the strategy's view in real time, without acting.
    prev_above = prev["ema9"] > prev["ema21"]
    curr_above = curr["ema9"] > curr["ema21"]
    if prev_above != curr_above:
        cross_str = "CROSS_UP" if curr_above else "CROSS_DOWN"
    else:
        cross_str = "no_cross"

    log.info(
        f"BAR  {label}  close={curr['close']:.2f}  "
        f"ema8={curr['ema9']:.2f}  ema21={curr['ema21']:.2f}  "
        f"adx={curr['adx']:.1f}  atr={curr['atr']:.2f}  "
        f"{cross_str}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

_console        = Console()
_dashboard_stop = threading.Event()


def _build_dashboard(ib, paper: bool, contract_label: str = "MGC") -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="stats",  size=5),
        Layout(name="latest", size=4),
        Layout(name="logs"),
    )

    # Header
    mode_str = "[bold red]LIVE[/bold red]" if not paper else "[bold yellow]PAPER[/bold yellow]"
    now_str  = datetime.now(ET).strftime("%a %b %d  %H:%M:%S")
    layout["header"].update(Panel(
        f"[bold gold1]🜚 ESCAFLOWNE[/bold gold1]   {mode_str}   "
        f"[dim]{now_str} ET  ·  WATCH MODE (no orders)[/dim]",
        box=box.SIMPLE,
    ))

    # Account stats
    try:
        usd_vals = [v for v in ib.accountValues() if v.currency == "USD"]
        nlv      = float(next((v.value for v in usd_vals if v.tag == "NetLiquidation"), 0))
        avail    = float(next((v.value for v in usd_vals if v.tag == "AvailableFunds"), 0))
    except Exception:
        nlv, avail = 0.0, 0.0

    stale = time.time() - _last_bar_time[0] if _bars_seen[0] > 0 else None
    stale_str = f"{stale:.0f}s ago" if stale is not None else "—"

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 3))
    for _ in range(4):
        t.add_column(min_width=14)
    t.add_row(
        "[dim]NLV[/dim]",        f"[bold]${nlv:,.2f}[/bold]",
        "[dim]AVAIL[/dim]",      f"[bold]${avail:,.2f}[/bold]",
    )
    t.add_row(
        "[dim]BARS SEEN[/dim]",  f"{_bars_seen[0]}",
        "[dim]LAST BAR[/dim]",   stale_str,
    )
    layout["stats"].update(Panel(t, title="[dim]account / feed[/dim]", box=box.ROUNDED))

    # Latest bar (just a static line — we don't have indicators at the dashboard
    # level since this is watch mode; the log panel shows the latest INFO line)
    layout["latest"].update(Panel(
        Text(f"Contract: {contract_label}  ·  Watching for 5-min bars...",
             style="dim"),
        title="[dim]status[/dim]", box=box.ROUNDED,
    ))

    # Logs
    log_table = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    log_table.add_column(style="dim", width=19, no_wrap=True)
    log_table.add_column(no_wrap=False)

    for level, msg in list(_log_buffer):
        parts   = msg.split("  ", 1)
        ts      = parts[0] if len(parts) > 1 else ""
        content = parts[1] if len(parts) > 1 else msg

        if level >= logging.ERROR:
            style = "bold red"
        elif level >= logging.WARNING:
            style = "yellow"
        elif "CROSS_UP" in content:
            style = "bold green"
        elif "CROSS_DOWN" in content:
            style = "bold red"
        elif "BAR" in content:
            style = "white"
        else:
            style = "dim white"

        log_table.add_row(ts, Text(content, style=style, overflow="fold"))

    layout["logs"].update(Panel(log_table, title="[dim]log[/dim]", box=box.ROUNDED))
    return layout


def _run_dashboard(ib_ref, paper, contract_label):
    """Background thread: refreshes the Rich dashboard ~2x/sec."""
    with Live(console=_console, refresh_per_second=2, screen=True) as live:
        while not _dashboard_stop.is_set():
            try:
                ib = ib_ref[0]
                if ib is not None:
                    live.update(_build_dashboard(ib, paper, contract_label))
            except Exception as e:
                log.warning(f"Dashboard error: {e}")
            time.sleep(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# State publisher — writes state_escaflowne.json every 10s
# ─────────────────────────────────────────────────────────────────────────────

def _publish_state_loop(ib_ref, paper, timer_ref):
    try:
        ib = ib_ref[0]
        if ib is not None and ib.isConnected():
            usd_vals = [v for v in ib.accountValues() if v.currency == "USD"]
            nlv      = float(next((v.value for v in usd_vals if v.tag == "NetLiquidation"), 0))

            stale = time.time() - _last_bar_time[0] if _bars_seen[0] > 0 else 999
            status = "watching" if stale < 600 else "stale"

            write_state(
                nlv=nlv,
                status=status,
                in_position=False,
                instrument="MGC",
                trades_today=0,
                paper=paper,
            )
    except Exception as e:
        log.warning(f"State publish failed: {e}")

    t = threading.Timer(10.0, _publish_state_loop, args=(ib_ref, paper, timer_ref))
    t.daemon = True
    t.start()
    timer_ref[0] = t


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", default=False,
                        help="Connect to LIVE port 4001 instead of paper 4002")
    args  = parser.parse_args()
    paper = not args.live

    if not paper:
        # Even in watch mode, double-confirm: watch mode + live = no harm, but
        # this is a habit-building safety net for when runner.py is added later.
        print("\n  WATCH MODE — no orders will be placed even in --live.")
        print("  Use --live for paper anyway? It only changes the connection port.")
        confirm = input("\n  Connect to LIVE Gateway (port 4001)? [y/N]: ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            sys.exit(0)

    log.info("=" * 60)
    log.info(f"  Escaflowne starting in {'PAPER' if paper else 'LIVE'} mode (WATCH ONLY)")
    log.info(f"  Instrument: MGC  ·  Strategy: NOT EVALUATED (no runner.py yet)")
    log.info("=" * 60)

    ib              = None
    feeds           = []
    ib_ref          = [None]
    state_timer_ref = [None]

    try:
        ib = connect(paper=paper)
        ib_ref[0] = ib

        contract = get_mgc_contract(ib)
        log.info(f"MGC contract: {contract.localSymbol}  expiry={contract.lastTradeDateOrContractMonth}")

        # Send Discord "BOT STARTED" alert
        alert_restart(paper=paper)

        # Subscribe to 5-min MGC bars
        feed = LiveFeed(ib=ib, contract=contract, on_bar=_on_bar_watch, label="MGC")
        feed.start()
        feeds.append(feed)
        log.info("MGC feed subscribed — watching for 5-min bars.")

        # Start dashboard thread
        dash_thread = threading.Thread(
            target=_run_dashboard, args=(ib_ref, paper, "MGC"), daemon=True,
        )
        dash_thread.start()

        # Start state publisher loop
        _publish_state_loop(ib_ref, paper, state_timer_ref)

        # ── Disconnection handler ─────────────────────────────────────────────
        def _on_disconnect():
            log.error("IBKR CONNECTION LOST")
            alert_disconnect(paper)

        ib.disconnectedEvent += _on_disconnect

        # ── Main loop — just keep the event loop alive ───────────────────────
        log.info("Watch mode active. Press Ctrl+C to stop.")
        try:
            util.run()
        except KeyboardInterrupt:
            log.info("Shutdown requested.")

    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    except ConnectionRefusedError:
        log.error(f"Cannot connect to IB Gateway on port "
                  f"{cfg.PORT_PAPER if paper else cfg.PORT_LIVE}. "
                  f"Is the Gateway running and API enabled?")
        sys.exit(1)
    except Exception as e:
        log.error(f"Fatal error in main(): {e}", exc_info=True)
        sys.exit(1)
    finally:
        _dashboard_stop.set()
        if state_timer_ref[0]:
            state_timer_ref[0].cancel()
        for feed in feeds:
            try:
                feed.stop()
            except Exception:
                pass
        if ib is not None and ib.isConnected():
            try:
                disconnect(ib)
            except Exception:
                pass
        alert_shutdown(paper, reason="watch mode stopped")
        log.info("Escaflowne stopped cleanly.")


if __name__ == "__main__":
    main()