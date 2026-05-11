"""
run_trade.py — Escaflowne TRADING entry point (v2, with Rich dashboard).

Direct port of Celeri's run.py pattern, adapted for single-instrument MGC.

Components (mirroring Celeri):
  - Rich Live dashboard in a daemon thread (screen=True, refresh 2x/s)
  - util.run() in main thread for ib_insync event loop
  - Reconnect loop with exponential backoff (5-300s, max 20 attempts)
  - Watchdog: forces reconnect if no bars for 10 min (WATCHDOG_TIMEOUT)
  - Account logger every 30s (writes ACCT line to log for external GUI parsing)
  - Heartbeat every 30 min (silent Discord ping with NLV/uptime)
  - Daily stat preservation across reconnects

Single-instrument differences from Celeri:
  - `contracts` dict → single `contract` (no MES/MNQ loop)
  - Account ID read from runner.account_id (Celeri hardcoded "U24215164")
  - Log file: escaflowne_live.log
  - sys.path safety insert prevents rogue celeri-repo from shadowing imports

Usage:
    .venv\\Scripts\\python.exe run_trade.py          # paper, port 4002
    .venv\\Scripts\\python.exe run_trade.py --live   # LIVE, requires confirmation
"""

# ── sys.path safety FIRST ───────────────────────────────────────────────────
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from dotenv import load_dotenv
load_dotenv()

import argparse
import asyncio
import collections
import logging
import os
import threading
import time
from datetime import datetime

import pytz

from ib_insync import util
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from live.broker import connect, disconnect, get_mgc_contract
from live.feed import LiveFeed
from live.runner import LiveRunner
from live.alerts import (
    alert_shutdown, alert_disconnect, alert_restart, alert_heartbeat
)

import config.live as cfg


ET = pytz.timezone("US/Eastern")


# ─────────────────────────────────────────────────────────────────────────────
# Log capture for the dashboard
# ─────────────────────────────────────────────────────────────────────────────

_log_buffer = collections.deque(maxlen=12)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            _log_buffer.append((record.levelno, msg))
        except Exception:
            pass


class SafeFileHandler(logging.FileHandler):
    def emit(self, record):
        for attempt in range(3):
            try:
                super().emit(record)
                return
            except (PermissionError, OSError):
                if attempt < 2:
                    time.sleep(0.1)


LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_PATH    = HERE / "escaflowne_live.log"

_buf_handler  = _BufferHandler()
_buf_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt=LOG_DATEFMT))

_file_handler = SafeFileHandler(LOG_PATH, mode="a", delay=True, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt=LOG_DATEFMT))

logging.basicConfig(level=logging.INFO, handlers=[_buf_handler, _file_handler])
logging.getLogger("ib_insync").setLevel(logging.ERROR)
logging.getLogger("eventkit").setLevel(logging.ERROR)
log = logging.getLogger("escaflowne")


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard builder
# ─────────────────────────────────────────────────────────────────────────────

_console = Console()


def _build_dashboard(ib, runner, paper: bool) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header",   size=3),
        Layout(name="stats",    size=5),
        Layout(name="position", size=5),
        Layout(name="logs"),
    )

    # ── Header ────────────────────────────────────────────────────────────
    mode_str = (
        "[bold red]LIVE[/bold red]" if not paper
        else "[bold yellow]PAPER[/bold yellow]"
    )
    now_str = datetime.now(ET).strftime("%a %b %d  %H:%M:%S")
    layout["header"].update(Panel(
        f"[bold gold1]🜚 ESCAFLOWNE[/bold gold1]   {mode_str}   "
        f"[dim]{now_str} ET[/dim]",
        box=box.SIMPLE,
    ))

    # ── Account stats ─────────────────────────────────────────────────────
    account_id = runner.account_id if runner else None
    try:
        if account_id is not None:
            tags = {v.tag: v.value for v in ib.accountValues()
                    if v.currency == "USD" and v.account == account_id}
        else:
            tags = {v.tag: v.value for v in ib.accountValues()
                    if v.currency == "USD"}
        nlv    = float(tags.get("NetLiquidation", 0))
        un_pnl = float(tags.get("UnrealizedPnL",  0))
    except Exception:
        nlv, un_pnl = 0.0, 0.0

    day_pnl = runner._daily_pnl          if runner else 0.0
    trades  = runner.state.trades_taken  if runner else 0
    limit   = runner._daily_loss_limit   if runner else 0.0
    max_tr  = getattr(cfg, "MAX_TRADES_SESSION", "∞")
    if max_tr is None:
        max_tr = "∞"
    killed  = runner._killed             if runner else False

    pnl_color = "green" if day_pnl >= 0 else "red"
    un_color  = "green" if un_pnl  >= 0 else "red"

    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 3))
    for _ in range(6):
        t.add_column(min_width=14)
    t.add_row(
        "[dim]NLV[/dim]",        f"[bold]${nlv:,.2f}[/bold]",
        "[dim]DAY P&L[/dim]",    f"[{pnl_color}]{day_pnl:+.2f}[/{pnl_color}]",
        "[dim]UNREALIZED[/dim]", f"[{un_color}]{un_pnl:+.2f}[/{un_color}]",
    )
    t.add_row(
        "[dim]TRADES[/dim]",     f"{trades} / {max_tr}",
        "[dim]LOSS LIMIT[/dim]", f"[red]{limit:.2f}[/red]",
        "[dim]STATUS[/dim]",
        "[bold red]HALTED[/bold red]" if killed else "[green]ACTIVE[/green]",
    )
    layout["stats"].update(Panel(t, title="[dim]account[/dim]", box=box.ROUNDED))

    # ── Position ──────────────────────────────────────────────────────────
    if runner and runner.state.in_position:
        sig   = runner._active_signal
        entry = runner._entry_fill_price or (sig.entry_price if sig else 0)
        qty   = runner._trade_qty

        if sig:
            direction_tag = (
                "[bold green] LONG [/bold green]" if sig.direction == "LONG"
                else "[bold red] SHORT [/bold red]"
            )
            pos_text = Text.assemble(
                direction_tag, f"  MGC  ×{qty}  @  {entry:.2f}\n",
                ("stop ", "dim"), (f"{sig.stop_price:.2f}", "red"),
                ("   exit ", "dim"), ("EMA cross down", "cyan"),
            )
        else:
            pos_text = Text(f"IN POSITION  MGC  ×{qty}", style="yellow")
    else:
        status   = runner.get_status() if runner else "waiting"
        kill_rsn = runner._kill_reason if (runner and runner._killed) else ""
        if kill_rsn:
            pos_text = Text(f"HALTED — {kill_rsn}", style="bold red")
        else:
            pos_text = Text(f"FLAT — {status}", style="dim")

    layout["position"].update(Panel(pos_text, title="[dim]position[/dim]", box=box.ROUNDED))

    # ── Logs ──────────────────────────────────────────────────────────────
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
        elif "SIGNAL" in content or "FILLED" in content:
            style = "bold green"
        elif "EXIT" in content:
            style = "cyan"
        else:
            style = "white"

        log_table.add_row(ts, Text(content, style=style, overflow="fold"))

    layout["logs"].update(Panel(log_table, title="[dim]log[/dim]", box=box.ROUNDED))
    return layout


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat — silent Discord ping every 30 min
# ─────────────────────────────────────────────────────────────────────────────

def _send_heartbeat(ib, runner_ref: list, paper: bool, timer_ref: list, start_time: float = 0):
    try:
        if ib.isConnected():
            runner = runner_ref[0]
            account_id = runner.account_id if runner else None
            if account_id is not None:
                tags = {v.tag: v.value for v in ib.accountValues()
                        if v.currency == "USD" and v.account == account_id}
            else:
                tags = {v.tag: v.value for v in ib.accountValues()
                        if v.currency == "USD"}
            nlv    = float(tags.get("NetLiquidation", 0))
            trades = runner.state.trades_taken if runner else 0
            secs   = int(time.time() - start_time) if start_time else 0
            hours  = secs // 3600
            mins   = (secs % 3600) // 60
            uptime = f"{hours}h {mins}m" if hours else f"{mins}m"
            alert_heartbeat(paper, nlv=nlv, trades_today=trades, uptime_str=uptime)
    except Exception as e:
        log.warning(f"Heartbeat failed: {e}")

    t = threading.Timer(1800, _send_heartbeat,
                        args=[ib, runner_ref, paper, timer_ref, start_time])
    t.daemon = True
    t.start()
    timer_ref[0] = t


# ─────────────────────────────────────────────────────────────────────────────
# Account logger — writes ACCT line to log file every 30s
# ─────────────────────────────────────────────────────────────────────────────

def _log_account(ib, runner_ref: list, timer_ref: list):
    try:
        if ib.isConnected():
            runner = runner_ref[0]
            account_id = runner.account_id if runner else None
            if account_id is not None:
                tags = {v.tag: v.value for v in ib.accountValues()
                        if v.currency == "USD" and v.account == account_id}
            else:
                tags = {v.tag: v.value for v in ib.accountValues()
                        if v.currency == "USD"}
            nlv     = float(tags.get("NetLiquidation", 0))
            un_pnl  = float(tags.get("UnrealizedPnL",  0))
            margin  = float(tags.get("MaintMarginReq", 0))
            day_pnl = runner._daily_pnl if runner else 0.0
            log.info(
                f"ACCT | NLV {nlv:,.2f} | "
                f"DayPNL {'+' if day_pnl >= 0 else ''}{day_pnl:,.2f} | "
                f"UnPNL {'+' if un_pnl >= 0 else ''}{un_pnl:,.2f} | "
                f"Margin {margin:,.2f}"
            )
    except Exception as e:
        log.warning(f"Account log failed: {e}")

    t = threading.Timer(30, _log_account, args=[ib, runner_ref, timer_ref])
    t.daemon = True
    t.start()
    timer_ref[0] = t


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard thread
# ─────────────────────────────────────────────────────────────────────────────

_dashboard_stop = threading.Event()


def _run_dashboard(ib_ref: list, runner_ref: list, paper: bool):
    with Live(
        _build_dashboard(ib_ref[0], runner_ref[0], paper),
        console=_console,
        refresh_per_second=2,
        screen=True,
    ) as live:
        while not _dashboard_stop.is_set():
            try:
                live.update(_build_dashboard(ib_ref[0], runner_ref[0], paper))
            except Exception:
                pass
            time.sleep(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# Reconnection logic
# ─────────────────────────────────────────────────────────────────────────────

MAX_RECONNECT_ATTEMPTS = 20
RECONNECT_BASE_DELAY   = 5
RECONNECT_MAX_DELAY    = 300
WATCHDOG_INTERVAL      = 60
WATCHDOG_TIMEOUT       = 600


def _reconnect_loop(paper: bool, old_runner):
    """Try to reconnect to IBKR with exponential backoff."""
    delay = RECONNECT_BASE_DELAY
    for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
        log.warning(f"Reconnect attempt {attempt}/{MAX_RECONNECT_ATTEMPTS} in {delay}s...")
        time.sleep(delay)
        try:
            ib = connect(paper=paper)
            contract = get_mgc_contract(ib)
            log.info(f"Reconnected MGC: {contract.localSymbol}")

            runner = LiveRunner(ib=ib, contract=contract, paper=paper)
            if old_runner is not None:
                # Preserve daily stats across reconnect (Celeri pattern)
                runner._daily_pnl         = old_runner._daily_pnl
                runner._daily_wins        = old_runner._daily_wins
                runner._daily_losses      = old_runner._daily_losses
                runner.state.trades_taken = old_runner.state.trades_taken
                if old_runner._killed:
                    runner._killed      = True
                    runner._kill_reason = old_runner._kill_reason
                log.info(
                    f"State restored: pnl={runner._daily_pnl:+.2f} "
                    f"trades={runner.state.trades_taken}"
                )

            feed = LiveFeed(
                ib=ib, contract=contract,
                on_bar=runner.on_bar, label="MGC",
                get_status=runner.get_status,
            )
            feed.start()
            feeds = [feed]

            def _on_error_reconnect(reqId, errorCode, errorString, advancedError=""):
                if errorCode in (1100, 1101, 10182):
                    log.error(f"Critical connectivity error {errorCode} — forcing reconnect")
                    try:
                        loop = asyncio.get_event_loop()
                        loop.call_soon_threadsafe(loop.stop)
                    except Exception:
                        pass
                elif errorCode == 1102:
                    log.info("Connectivity restored — checking positions in 30s")
                    def _delayed_check():
                        try:
                            runner._check_unprotected_positions()
                        except Exception as e:
                            log.error(f"Delayed position check failed: {e}")
                    threading.Timer(30, _delayed_check).start()

            ib.errorEvent += _on_error_reconnect

            log.info(f"Reconnected on attempt {attempt}")
            return ib, contract, runner, feeds
        except Exception as e:
            log.warning(f"Reconnect {attempt} failed: {e}")
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    raise ConnectionError(f"Failed to reconnect after {MAX_RECONNECT_ATTEMPTS} attempts")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", default=False)
    args  = parser.parse_args()
    paper = not args.live

    if not paper:
        print()
        print("  ⚠️  ⚠️  ⚠️   LIVE TRADING REQUESTED   ⚠️  ⚠️  ⚠️")
        print()
        print("  This will place REAL ORDERS on Escaflowne's live account.")
        print()
        confirm = input("  Type 'YES I UNDERSTAND' to proceed: ")
        if confirm.strip() != "YES I UNDERSTAND":
            print("Aborted.")
            sys.exit(0)

    log.info(f"{'='*60}")
    log.info(f"  Escaflowne starting in {'PAPER' if paper else 'LIVE'} mode")
    log.info(f"  Instrument: MGC (single-instrument)")
    log.info(
        f"  Params: EMA({cfg.EMA_FAST},{cfg.EMA_SLOW}) "
        f"ADX>={cfg.ADX_TREND_THRESHOLD} ADX_rising={cfg.ADX_MUST_RISE} "
        f"ATR×{cfg.ATR_STOP_MULT} min_stop={cfg.MIN_STOP_POINTS_MGC}pts"
    )
    log.info(f"{'='*60}")

    ib             = None
    feeds          = []
    runner         = None
    watchdog_ref   = [None]
    acct_timer_ref = [None]
    hb_timer_ref   = [None]
    _start_time    = time.time()

    # Shared refs for dashboard thread to read
    ib_ref     = [None]
    runner_ref = [None]

    try:
        ib       = connect(paper=paper)
        contract = get_mgc_contract(ib)
        log.info(f"MGC contract: {contract.localSymbol}  conId={contract.conId}")

        runner = LiveRunner(ib=ib, contract=contract, paper=paper)
        ib_ref[0]     = ib
        runner_ref[0] = runner

        feed = LiveFeed(
            ib=ib, contract=contract,
            on_bar=runner.on_bar, label="MGC",
            get_status=runner.get_status,
        )
        feed.start()
        feeds.append(feed)

        # Start dashboard thread
        dash_thread = threading.Thread(
            target=_run_dashboard, args=(ib_ref, runner_ref, paper), daemon=True
        )
        dash_thread.start()

        # Start account logger (every 30s)
        _log_account(ib, runner_ref, acct_timer_ref)

        # Start heartbeat (every 30 min)
        _send_heartbeat(ib, runner_ref, paper, hb_timer_ref, _start_time)

        # Watchdog: forces reconnect if no bars for WATCHDOG_TIMEOUT seconds
        def _watchdog_check():
            try:
                if runner and hasattr(runner, "_last_bar_time"):
                    stale = time.time() - runner._last_bar_time
                    if stale > WATCHDOG_TIMEOUT:
                        log.error(f"WATCHDOG: No bars for {stale:.0f}s — forcing reconnect")
                        try: ib.disconnect()
                        except Exception: pass
                        try:
                            loop = asyncio.get_event_loop()
                            loop.call_soon_threadsafe(loop.stop)
                        except Exception: pass
                        return
            except Exception as e:
                log.warning(f"Watchdog check failed: {e}")
            t = threading.Timer(WATCHDOG_INTERVAL, _watchdog_check)
            t.daemon = True
            t.start()
            watchdog_ref[0] = t

        _watchdog_check()

        def _on_disconnect():
            log.error("IBKR CONNECTION LOST")
            alert_disconnect(paper)
            try:
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

        ib.disconnectedEvent += _on_disconnect

        log.info("Live feed active — waiting for bars.")

        # Main loop with reconnect handling
        while True:
            try:
                util.run()
                # util.run() returns when loop.stop() is called — typically
                # from disconnect or watchdog. Clean up and reconnect.
                for feed in feeds:
                    try: feed.stop()
                    except Exception: pass
                feeds.clear()
                if watchdog_ref[0]: watchdog_ref[0].cancel()

                if not ib.isConnected():
                    log.warning("Connection lost — reconnecting...")
                else:
                    log.warning("Watchdog triggered — reconnecting...")
                    try: ib.disconnect()
                    except Exception: pass

                ib, contract, runner, feeds = _reconnect_loop(paper, old_runner=runner)
                ib_ref[0]     = ib
                runner_ref[0] = runner
                _log_account(ib, runner_ref, acct_timer_ref)
                _send_heartbeat(ib, runner_ref, paper, hb_timer_ref, _start_time)
                _watchdog_check()
                ib.disconnectedEvent += _on_disconnect
                log.info("Reconnected — resuming")
                continue

            except KeyboardInterrupt:
                log.info("Shutdown requested...")
                break

    except KeyboardInterrupt:
        log.info("Shutdown requested...")
    except ConnectionRefusedError:
        log.error(
            "Cannot connect to IB Gateway. "
            "Make sure Gateway is open on port 4001 (live) or 4002 (paper)."
        )
        sys.exit(1)
    except ConnectionError as e:
        log.error(f"Gave up reconnecting: {e}")
        sys.exit(1)
    finally:
        _dashboard_stop.set()
        if watchdog_ref[0]:   watchdog_ref[0].cancel()
        if acct_timer_ref[0]: acct_timer_ref[0].cancel()
        if hb_timer_ref[0]:   hb_timer_ref[0].cancel()
        for feed in feeds:
            try: feed.stop()
            except Exception: pass
        if ib and ib.isConnected():
            disconnect(ib)
        log.info("Escaflowne stopped cleanly.")
        alert_shutdown(paper)


if __name__ == "__main__":
    main()