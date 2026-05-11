"""
live/runner.py — Escaflowne (Celeri-on-MGC)

Live trading brain for the MGC EMA crossover strategy.

Strategy (single-instrument, MGC only):
  - Long-only EMA8/EMA21 crossover with ADX regime filter (and ADX_MUST_RISE)
  - Enter on EMA8 crossing above EMA21 (ADX >= cfg.ADX_TREND_THRESHOLD, ADX rising)
  - Stop: ATR×4 (cfg.ATR_STOP_MULT), placed as STP LMT at IBKR (works overnight)
  - Exit: EMA8 crosses below EMA21 → market close
  - Friday 16:59 ET force-close to avoid weekend gap risk

Kill switches:
  DAILY_LOSS_PCT     — halt if daily P&L drops below X% of account equity
  MAX_TRADES_SESSION — halt after N trades today
Both reset automatically at next session boundary.

CSV trade log (trades_escaflowne.csv):
  Same schema as Celeri's trades_live.csv. The `time_et` legacy column is
  preserved for backwards compatibility with any dashboards.

Ported scar tissue from Celeri (DO NOT remove, weaken, or "simplify" these):

Orphan-stop invariants (Celeri incident 2026-04-23: rejected long turned
into naked short due to uncancelled child stop):
  1. Every market-close path MUST cancel resting protective orders first
     via _cancel_protectives_for(). This includes CROSS_EXIT, EOD, and any
     future market-close path.
  2. When a parent entry gets rejected or cancelled, the child stop is
     cancelled explicitly in _on_error / _on_cancel.
  3. On bot startup, _cancel_orphans() scans for any protective order
     without a matching open position and cancels it.

Cross-exit double-fire guard (Celeri incident 2026-05-07: cross-exit market
sell sent on two consecutive bars before first fill landed, producing naked
short of -1 contracts caught by unprotected-position kill switch):
  - _cross_exit_sent flag prevents firing a second cross-exit market close
    while the first is still pending. Cleared when position detected closed
    (_handle_polled_exit) or by emergency flatten paths.

Position adoption (Celeri incident 2026-05-08: overnight winner +$128.75
was not logged because bot restarted during IBKR daily reset window and
lost _entry_fill_price / _active_signal. Position itself was managed
correctly via _sync_position + IBKR-side stop, but _handle_polled_exit
hit early-return branch on exit):
  - _sync_position calls _adopt_position when pos != 0 with no _active_signal.
  - Backfills entry price from ib.fills() (or pos.avgCost fallback),
    stop price from resting STP order, synthesizes Signal(reason="ADOPTED").
  - After adoption, _handle_polled_exit logs the trade and fires alerts.
"""

import csv
import logging
import os
import time
from datetime import datetime
from threading import Lock

import pytz
from ib_insync import IB, Order, MarketOrder

import config.live as cfg
from strategy.signals import SessionState, Signal, generate_signal
from live.broker import (
    has_open_position,
    get_position_size,
    cancel_all_orders,
)
from live.alerts import (
    alert_signal,
    alert_fill,
    alert_exit,
    alert_kill_switch,
    alert_daily_summary,
    alert_restart,
    alert_unprotected,
)
from live.state_publisher import write_state

log = logging.getLogger("escaflowne.runner")

ET = pytz.timezone("US/Eastern")

# Throttle restart alerts (avoid spam on rapid restarts within 5 min)
_last_restart_alert = 0

# ── MGC contract specifics ───────────────────────────────────────────────────
# Single-instrument bot, so these are constants rather than per-label lookups.
MGC_POINT_VALUE = 10.0   # $10/point for MGC (vs $5 MES, $2 MNQ)
MGC_TICK_SIZE   = 0.10   # $0.10 per tick
MGC_SLIP_BUF    = 0.40   # 4 ticks slip buffer for STP LMT (Celeri used 2.0 MES / 4.0 MNQ)
MGC_EXCHANGE    = "COMEX"
LABEL           = "MGC"  # used everywhere Celeri used `label` parameter

# Protective order types we track for orphan/cleanup purposes.
PROTECTIVE_ORDER_TYPES = ("STP", "STP LMT", "LMT", "TRAIL", "TRAIL LIMIT")

# Stop-only protective order types (used when reading stop price during adoption).
STOP_ORDER_TYPES = ("STP", "STP LMT")

# CSV schema. 'time_et' is the LEGACY column (exit time HH:MM:SS) — kept for
# backwards compatibility with any Celeri-era dashboards. New code should use
# entry_time_et/exit_time_et which are unambiguous ISO ET timestamps.
CSV_HEADER = [
    "date",            # entry date in ET (YYYY-MM-DD)
    "time_et",         # legacy: exit HH:MM:SS in ET — kept for backcompat
    "entry_time_et",   # ISO ET timestamp of entry fill
    "exit_time_et",    # ISO ET timestamp of exit
    "instrument",      # always "MGC" for Escaflowne
    "reason",
    "entry_price",
    "exit_price",
    "stop_price",
    "qty",
    "pnl_points",
    "pnl_dollars",
    "result",
    "mode",
]


def _round_tick(price: float, tick: float = MGC_TICK_SIZE) -> float:
    return round(round(price / tick) * tick, 10)


def _format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


def _should_force_close(now_et, cfg) -> bool:
    """
    Decide if current ET timestamp should trigger session-end force close.

    Modes (cfg.ENABLE_EOD_EXIT):
      True          → close at 18:00 ET every day
      False         → never force-close
      "friday_only" → close at 16:59 ET Friday (before weekend)
    """
    eod_mode = getattr(cfg, "ENABLE_EOD_EXIT", "friday_only")
    if eod_mode is True:
        return now_et.hour == 18 and now_et.minute == 0
    if eod_mode == "friday_only":
        return now_et.weekday() == 4 and now_et.hour == 16 and now_et.minute == 59
    return False


# ═════════════════════════════════════════════════════════════════════════════
# LiveRunner — single-instrument MGC trading brain
# ═════════════════════════════════════════════════════════════════════════════

class LiveRunner:
    def __init__(self, ib: IB, contract, paper: bool = True):
        """
        Single-instrument runner for MGC.

        Args:
            ib: connected ib_insync.IB instance
            contract: qualified MGC Future contract (from get_mgc_contract)
            paper: True for paper trading, False for live
        """
        self.ib       = ib
        self.contract = contract
        self.paper    = paper
        self.state    = SessionState()
        self._last_date = None
        self._lock    = Lock()

        # Account discovery — use the first managed account rather than hardcoding
        # (Celeri hardcoded ACCOUNT_ID = "U24215164"; we read it from IBKR so the
        # same code works for paper account DUO209386 or escaflowne live U25212424)
        accts = ib.managedAccounts()
        if not accts:
            raise RuntimeError("No managed accounts found on IBKR connection")
        self.account_id = accts[0]
        log.info(f"Account: {self.account_id}  ({'PAPER' if paper else 'LIVE'})")

        self._killed      = False
        self._kill_reason = ""

        self._daily_pnl          = 0.0
        self._daily_wins         = 0
        self._daily_losses       = 0
        self._realized_pnl_base  = self._snapshot_realized_pnl()

        self._daily_loss_limit = self._calc_loss_limit()

        # Active-trade tracking (single instrument, but keep _active_* naming
        # for consistency with Celeri's logging/scar-tissue patterns)
        self._active_contract  = None
        self._active_signal    = None
        self._last_skip        = "waiting"
        self._entry_fill_price = None
        self._entry_time       = None   # epoch float (used for duration calc)
        self._entry_dt_et      = None   # tz-aware datetime in ET (used for CSV)
        self._trade_qty        = 1
        self._cooldown         = False
        self._cross_exit_sent  = False  # guard against double cross-exit fire
        self._last_bar_time    = time.time()

        # Latency tracking
        self._last_latency = {}
        self._latency_csv  = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "reports", "latency_escaflowne.csv")
        )
        self._ensure_latency_csv()

        # Trade log (separate filename from Celeri's trades_live.csv)
        self._csv_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "trades_escaflowne.csv")
        )
        self._ensure_csv()

        # Log loaded config so log archaeology is easier
        log.info(
            f"Config: ALLOWED_HOURS={getattr(cfg, 'ALLOWED_HOURS', None)}  "
            f"ADX>={cfg.ADX_TREND_THRESHOLD} ADX_rising={cfg.ADX_MUST_RISE}  "
            f"EMA({cfg.EMA_FAST},{cfg.EMA_SLOW})  ATR×{cfg.ATR_STOP_MULT}  "
            f"min_stop={cfg.MIN_STOP_POINTS_MGC}pts  "
            f"DAILY_LOSS_PCT={getattr(cfg, 'DAILY_LOSS_PCT', 0.04)}  "
            f"MAX_TRADES={getattr(cfg, 'MAX_TRADES_SESSION', '∞')}"
        )

        self._cancel_orphans()
        self._check_unprotected_positions()

        global _last_restart_alert
        now = time.time()
        if now - _last_restart_alert > 300:
            _last_restart_alert = now
            alert_restart(paper)

    # ── Startup helpers ──────────────────────────────────────────────────────

    def _acct_values(self):
        """Account values for our specific account ID."""
        return [v for v in self.ib.accountValues() if v.account == self.account_id]

    def _snapshot_realized_pnl(self) -> float:
        try:
            for v in self._acct_values():
                if v.tag == "RealizedPnL" and v.currency == "USD":
                    baseline = float(v.value)
                    log.info(f"RealizedPnL baseline: ${baseline:,.2f}")
                    return baseline
        except Exception as e:
            log.warning(f"Could not snapshot RealizedPnL: {e}")
        return 0.0

    def _cancel_orphans(self):
        """Cancel orders with no matching position. See module docstring."""
        try:
            self.ib.sleep(2)

            open_trades = self.ib.openTrades()
            if not open_trades:
                log.info("No open orders on startup — clean slate")
                return

            positions = {pos.contract.conId: pos
                         for pos in self.ib.positions() if pos.position != 0}
            cancelled_count = 0
            preserved_count = 0

            for trade in open_trades:
                conid    = trade.contract.conId
                order_id = trade.order.orderId
                otype    = trade.order.orderType
                parent   = trade.order.parentId
                symbol   = trade.contract.localSymbol

                if conid in positions:
                    log.info(
                        f"Preserving order {order_id} ({otype}) — "
                        f"protective for existing {symbol} position"
                    )
                    preserved_count += 1
                    continue

                kind = "child protective" if parent != 0 else "standalone entry"
                log.warning(
                    f"ORPHAN DETECTED — cancelling {kind} order {order_id} "
                    f"({otype}, parentId={parent}) for {symbol} — no position exists"
                )
                try:
                    self.ib.cancelOrder(trade.order)
                    cancelled_count += 1
                except Exception as e:
                    log.error(f"Failed to cancel orphan order {order_id}: {e}")

            log.info(
                f"Orphan audit complete: {cancelled_count} cancelled, "
                f"{preserved_count} preserved"
            )
        except Exception as e:
            log.error(f"Orphan cancel pass failed: {e}", exc_info=True)

    def _check_unprotected_positions(self):
        try:
            positions = [pos for pos in self.ib.positions() if pos.position != 0]
            if not positions:
                log.info("No positions on startup — starting fresh")
                return
            open_orders = {t.contract.conId: t for t in self.ib.openTrades()}
            for pos in positions:
                if pos.contract.conId not in open_orders:
                    log.error(f"UNPROTECTED POSITION DETECTED")
                    log.error(
                        f"{pos.contract.localSymbol}: {pos.position} contracts "
                        f"with NO PROTECTIVE ORDERS"
                    )
                    self._market_close(pos.contract, int(pos.position), "UNPROTECTED_STARTUP")
                    alert_unprotected(pos.contract.localSymbol, int(pos.position), self.paper)
                    self._trigger_kill(
                        f"UNPROTECTED {pos.contract.localSymbol} — "
                        f"emergency flatten ({pos.position} contracts)"
                    )
                else:
                    log.info(f"Position {pos.contract.localSymbol} has protective orders — OK")
        except Exception as e:
            log.error(f"Could not check unprotected positions: {e}", exc_info=True)

    def _calc_loss_limit(self) -> float:
        try:
            for v in self._acct_values():
                if v.tag == "NetLiquidation" and v.currency == "USD":
                    equity = float(v.value)
                    daily_loss_pct = getattr(cfg, "DAILY_LOSS_PCT", 0.04)
                    limit  = -abs(equity * daily_loss_pct)
                    log.info(
                        f"Daily loss limit: ${limit:.2f}  "
                        f"({daily_loss_pct*100:.1f}% of ${equity:,.0f})"
                    )
                    return limit
        except Exception as e:
            log.warning(f"Could not fetch account equity: {e} — using fallback -$200")
        return -200.0

    # ── Position adoption ────────────────────────────────────────────────────

    def _adopt_position(self, contract, pos_size: int) -> bool:
        """
        Synthesize internal state for a position the bot didn't open itself.

        Triggered when _sync_position detects pos != 0 with _active_signal == None.
        Common causes:
          - Bot restarted while a position was open (e.g. IBKR daily reset)
          - _on_fill callback never fired due to reconnect timing
          - (Future) Manual entry via TWS

        Reads from IBKR:
          - Entry price: most recent BUY fill matching contract (or pos.avgCost)
          - Entry time: fill's execution.time (or now)
          - Stop price: resting STP/STP LMT order's auxPrice
          - Quantity: pos_size (absolute value)

        After successful adoption, _handle_polled_exit on next position close
        will log the trade and fire alerts normally.

        Returns True on success, False if adoption failed (state remains None).
        """
        try:
            entry_price = None
            entry_dt    = None

            # ── Entry price: prefer most recent BUY fill ──────────────────────
            try:
                fills = self.ib.fills()
                buy_fills = [
                    f for f in fills
                    if LABEL in f.contract.localSymbol
                    and f.execution.side == "BOT"  # IBKR uses BOT/SLD
                ]
                if buy_fills:
                    buy_fills.sort(key=lambda f: f.execution.time)
                    last_buy = buy_fills[-1]
                    entry_price = float(last_buy.execution.price)
                    fill_time_utc = last_buy.execution.time
                    if fill_time_utc.tzinfo is None:
                        fill_time_utc = pytz.UTC.localize(fill_time_utc)
                    entry_dt = fill_time_utc.astimezone(ET)
                    log.info(
                        f"  ADOPT: entry from ib.fills() — price={entry_price} "
                        f"time={entry_dt.strftime('%Y-%m-%d %H:%M:%S')} ET"
                    )
            except Exception as e:
                log.warning(f"  ADOPT: ib.fills() lookup failed: {e}")

            # Fallback: use pos.avgCost
            if entry_price is None:
                for p in self.ib.positions():
                    if p.contract.conId == contract.conId and p.position != 0:
                        # avgCost for futures is in $ per contract (multiplier applied)
                        entry_price = float(p.avgCost) / MGC_POINT_VALUE
                        log.info(
                            f"  ADOPT: entry from pos.avgCost fallback — "
                            f"price={entry_price:.2f}"
                        )
                        break

            if entry_price is None:
                log.error(f"  ADOPT FAILED — could not determine entry price for MGC")
                return False

            if entry_dt is None:
                entry_dt = datetime.now(ET)
                log.info(f"  ADOPT: entry_dt fallback to now")

            # ── Stop price: read from resting STP order ──────────────────────
            stop_price = None
            try:
                for t in self.ib.openTrades():
                    if t.contract.conId != contract.conId:
                        continue
                    if t.order.orderType not in STOP_ORDER_TYPES:
                        continue
                    if t.order.action != "SELL":
                        continue
                    stop_price = float(t.order.auxPrice)
                    log.info(
                        f"  ADOPT: stop from resting order {t.order.orderId} "
                        f"({t.order.orderType}) — stop={stop_price}"
                    )
                    break
            except Exception as e:
                log.warning(f"  ADOPT: stop lookup failed: {e}")

            if stop_price is None:
                # No resting stop found — _emergency_flatten_if_unprotected
                # should fire next bar. Synthesize state with sentinel stop.
                stop_price = entry_price - 10.0  # sentinel; should not be hit
                log.error(
                    f"  ADOPT WARNING — no resting stop found for MGC position. "
                    f"Using sentinel stop={stop_price}. "
                    f"Emergency flatten should fire next bar."
                )

            # ── Synthesize state ─────────────────────────────────────────────
            self._active_contract  = contract
            self._active_signal    = Signal(
                direction="LONG",
                entry_price=entry_price,
                stop_price=stop_price,
                reason="ADOPTED",
            )
            self._entry_fill_price = entry_price
            self._entry_dt_et      = entry_dt
            self._entry_time       = time.time()  # best-effort
            self._trade_qty        = abs(int(pos_size))

            log.warning(
                f"  ADOPTED — MGC LONG x{self._trade_qty} "
                f"@ {entry_price:.2f}  stop={stop_price:.2f}  "
                f"(source: external entry / restart recovery)"
            )
            return True

        except Exception as e:
            log.error(f"  ADOPT failed with exception: {e}", exc_info=True)
            return False

    # ── Protective-order cancellation helper ─────────────────────────────────

    def _cancel_protectives_for(self, contract, reason: str) -> int:
        """Cancel resting STP/LMT/TRAIL orders before a market close.

        Critical safety: every market-close path MUST call this to prevent
        orphan stops becoming naked shorts. See module docstring.
        """
        try:
            cancelled = 0
            for t in self.ib.openTrades():
                if t.contract.conId != contract.conId:
                    continue
                if t.order.orderType not in PROTECTIVE_ORDER_TYPES:
                    continue
                try:
                    self.ib.cancelOrder(t.order)
                    cancelled += 1
                    log.warning(
                        f"  Cancelled protective {t.order.orderType} order "
                        f"{t.order.orderId} for MGC before {reason}"
                    )
                except Exception as e:
                    log.error(
                        f"  Failed to cancel protective {t.order.orderId} "
                        f"for MGC before {reason}: {e}",
                        exc_info=True
                    )
            if cancelled > 0:
                self.ib.sleep(0.5)
            return cancelled
        except Exception as e:
            log.error(f"  _cancel_protectives_for({reason}) failed: {e}", exc_info=True)
            return 0

    # ── State publishing ─────────────────────────────────────────────────────

    def _publish_state(self, adx=None, current_price=None):
        """Write current bot state to state_escaflowne.json for monitors."""
        nlv = None
        try:
            for v in self._acct_values():
                if v.tag == "NetLiquidation" and v.currency == "USD":
                    nlv = float(v.value)
                    break
        except Exception:
            pass

        pos_pnl = None
        if self.state.in_position:
            # Prefer live computation from price — IBKR's UnrealizedPnL refreshes
            # too slowly (often 30s+) and stays stale during fast moves.
            if (self._entry_fill_price is not None
                    and current_price is not None):
                pos_pnl = (
                    (current_price - self._entry_fill_price)
                    * MGC_POINT_VALUE
                    * self._trade_qty
                )
            else:
                try:
                    for v in self._acct_values():
                        if v.tag == "UnrealizedPnL" and v.currency == "USD":
                            pos_pnl = float(v.value)
                            break
                except Exception:
                    pass

        write_state(
            nlv=nlv,
            day_pnl=self._daily_pnl,
            status=self._last_skip,
            in_position=self.state.in_position,
            adx=adx,
            current_price=current_price,
            position_pnl=pos_pnl,
            instrument="MGC",
            trades_today=self.state.trades_taken,
            paper=self.paper,
            killed=self._killed,
            kill_reason=self._kill_reason,
        )

    # ── Latency CSV ──────────────────────────────────────────────────────────

    def _ensure_latency_csv(self):
        os.makedirs(os.path.dirname(self._latency_csv), exist_ok=True)
        if not os.path.exists(self._latency_csv):
            with open(self._latency_csv, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "date", "time_et", "instrument",
                    "bar_to_signal_ms",
                    "signal_to_order_prep_ms",
                    "order_prep_to_sent_ms",
                    "order_to_fill_ms",
                    "bar_to_fill_total_ms",
                ])
            log.info(f"Latency log created: {self._latency_csv}")

    def _log_latency(self, metrics: dict):
        try:
            now_et = datetime.now(ET)
            with open(self._latency_csv, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    now_et.strftime("%Y-%m-%d"),
                    now_et.strftime("%H:%M:%S"),
                    "MGC",
                    metrics.get("bar_to_signal_ms", ""),
                    metrics.get("signal_to_order_prep_ms", ""),
                    metrics.get("order_prep_to_sent_ms", ""),
                    metrics.get("order_to_fill_ms", ""),
                    metrics.get("bar_to_fill_total_ms", ""),
                ])
        except Exception as e:
            log.warning(f"Could not write latency CSV: {e}")

    def get_last_latency(self) -> dict:
        return dict(self._last_latency)

    # ── Kill switch ──────────────────────────────────────────────────────────

    def _check_kill_switches(self) -> bool:
        if self._killed:
            return True
        if self._daily_pnl <= self._daily_loss_limit:
            self._trigger_kill(
                f"daily loss limit hit  "
                f"(P&L={self._daily_pnl:+.2f}  limit={self._daily_loss_limit:+.2f})"
            )
            return True
        max_trades = getattr(cfg, "MAX_TRADES_SESSION", None)
        if max_trades is not None and self.state.trades_taken >= max_trades:
            self._trigger_kill(
                f"max trades reached  ({self.state.trades_taken}/{max_trades})"
            )
            return True
        return False

    def _trigger_kill(self, reason: str):
        if self._killed:
            return
        self._killed      = True
        self._kill_reason = reason
        log.warning(f"KILL SWITCH — {reason}")
        try:
            cancel_all_orders(self.ib)
        except Exception as e:
            log.error(f"Failed to cancel orders on kill: {e}")
        alert_kill_switch(reason, self._daily_pnl, self.state.trades_taken, self.paper)

    def get_status(self) -> str:
        if self._killed:
            return f"HALTED: {self._kill_reason}"
        return self._last_skip

    # ── Market close helper ──────────────────────────────────────────────────

    def _market_close(self, contract, qty: int, reason: str):
        """Send market order to flatten. Callers MUST cancel protectives first."""
        try:
            if qty == 0:
                return
            action = "BUY" if qty < 0 else "SELL"
            contract.exchange = MGC_EXCHANGE
            order = MarketOrder(action, abs(qty))
            order.tif = "GTC"
            order.outsideRth = True
            order.account = self.account_id
            self.ib.placeOrder(contract, order)
            log.info(
                f"  MARKET CLOSE placed — {reason} — "
                f"{action} {abs(qty)} {contract.localSymbol}"
            )
        except Exception as e:
            log.error(f"Market close failed ({reason}): {e}", exc_info=True)

    # ── Position sync & exit handling ────────────────────────────────────────

    def _sync_position(self, contract) -> bool:
        try:
            pos = get_position_size(self.ib, contract)
        except Exception as e:
            log.warning(f"Could not get position size: {e}")
            return self.state.in_position

        if pos != 0:
            # Adopt position state if we have none (e.g. after restart, missed
            # callback). See _adopt_position docstring for full rationale.
            if self._active_signal is None:
                self._adopt_position(contract, pos)

            self.state.in_position = True
            if self._last_skip == "pending_fill":
                self._last_skip = "in_position"
                log.info(f"  MGC FILL CONFIRMED via polling (callback missed)")
            return True

        if self.state.in_position:
            log.info(f"  MGC position closed (detected via poll)")
            self._handle_polled_exit()

        self.state.in_position = False
        self._active_contract  = None
        return False

    def _handle_polled_exit(self):
        signal   = self._active_signal
        entry_px = self._entry_fill_price or (signal.entry_price if signal else None)

        if signal is None or entry_px is None:
            log.warning(f"  MGC exit detected but no signal/entry data — skipping P&L calc")
            self._active_signal    = None
            self._entry_fill_price = None
            self._entry_dt_et      = None
            self._cross_exit_sent  = False
            self._last_skip        = "no_cross"
            return

        exit_price = self._get_last_execution_price()
        if exit_price is None:
            log.warning(f"  MGC could not determine exit price from fills")
            exit_price = entry_px

        pnl_points  = exit_price - entry_px
        pnl_dollars = pnl_points * MGC_POINT_VALUE * self._trade_qty

        result = "WIN" if pnl_dollars > 0 else "LOSS"
        self._daily_pnl += pnl_dollars

        try:
            for v in self._acct_values():
                if v.tag == "RealizedPnL" and v.currency == "USD":
                    self._realized_pnl_base = float(v.value)
                    break
        except Exception:
            pass

        if result == "WIN":
            self._daily_wins += 1
        else:
            self._daily_losses += 1

        duration_str = ""
        if self._entry_time:
            duration_str = _format_duration(time.time() - self._entry_time)

        nlv = 0.0
        try:
            for v in self._acct_values():
                if v.tag == "NetLiquidation" and v.currency == "USD":
                    nlv = float(v.value)
                    break
        except Exception:
            pass

        log.info(
            f"  MGC EXIT — LONG  "
            f"entry={entry_px}  exit={exit_price}  "
            f"pnl_pts={pnl_points:+.2f}  P&L=${pnl_dollars:+.2f}  ({result})  "
            f"daily_pnl={self._daily_pnl:+.2f}  duration={duration_str}"
        )

        self._log_trade(signal, entry_px, exit_price,
                        self._trade_qty, result, pnl_dollars)

        alert_exit(
            "MGC", "LONG", entry_px, exit_price,
            pnl_dollars, result, self._daily_pnl, self.paper,
            qty=self._trade_qty, duration_str=duration_str, nlv=nlv
        )

        if self._active_contract is not None:
            self._cancel_protectives_for(self._active_contract, "POST_EXIT_SWEEP")

        self._active_signal    = None
        self._entry_fill_price = None
        self._entry_time       = None
        self._entry_dt_et      = None
        self._cross_exit_sent  = False
        self._last_skip        = "no_cross"
        self._check_kill_switches()

    def _get_last_execution_price(self) -> float | None:
        try:
            fills = self.ib.fills()
            relevant = [f for f in fills if LABEL in f.contract.localSymbol]
            if not relevant:
                return None
            relevant.sort(key=lambda f: f.execution.time)
            if self._entry_fill_price is not None:
                exit_fills = [
                    f for f in relevant
                    if abs(f.execution.price - self._entry_fill_price) > 0.01
                ]
                if exit_fills:
                    return exit_fills[-1].execution.price
            if len(relevant) >= 2:
                return relevant[-1].execution.price
            return None
        except Exception as e:
            log.warning(f"Could not fetch execution price: {e}")
        return None

    def _emergency_flatten_if_unprotected(self, contract):
        try:
            pos = get_position_size(self.ib, contract)
            if pos == 0:
                return
            open_orders = [t for t in self.ib.openTrades() if t.contract.conId == contract.conId]
            if not open_orders:
                log.error(f"UNPROTECTED POSITION: MGC {pos} contracts with NO STOPS!")
                self._market_close(contract, int(pos), "UNPROTECTED")
                self.state.in_position = False
                self._active_contract  = None
                self._cross_exit_sent  = False
                alert_unprotected("MGC", pos, self.paper)
                self._trigger_kill(f"UNPROTECTED MGC — emergency flatten ({pos} contracts)")
        except Exception as e:
            log.error(f"Emergency flatten check failed: {e}")

    # ── Main bar callback ────────────────────────────────────────────────────

    def on_bar(self, prev, curr, label: str = "MGC"):
        """
        Called by LiveFeed when a 5-min bar completes.

        prev, curr: Pandas Series rows with OHLCV + ema9/ema21/adx/atr columns.
        label: kept as a param for signature compatibility with Celeri's runner;
               we ignore it since this bot is MGC-only.
        """
        t_bar_received = time.time()
        self._last_bar_time = t_bar_received

        with self._lock:
            contract = self.contract

            self._emergency_flatten_if_unprotected(contract)

            now_et = curr.name.tz_convert(ET) if curr.name.tzinfo else curr.name

            today = now_et.date()
            if today != self._last_date:
                self._reset_session(now_et)
                self._last_date = today

            self.state.session_bars += 1

            if _should_force_close(now_et, cfg):
                pos = get_position_size(self.ib, contract)
                if pos != 0:
                    log.warning(f"  MGC FORCE CLOSE (session end) — flattening")
                    self._cancel_protectives_for(contract, "EOD_FORCE_CLOSE")
                    self._market_close(contract, int(pos), "EOD")

            if self._check_kill_switches():
                self._last_skip = f"HALTED: {self._kill_reason}"
                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))
                return

            if self._cooldown:
                self._cooldown  = False
                self._last_skip = "cooldown"
                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))
                return

            active_contract = self._active_contract or contract
            in_pos = self._sync_position(active_contract)

            regime    = "TREND" if curr["adx"] >= cfg.ADX_TREND_THRESHOLD else "RANGE"
            ema_cross = (prev["ema9"] > prev["ema21"]) != (curr["ema9"] > curr["ema21"])
            cross_dir = ""
            if ema_cross:
                cross_dir = "LONG" if curr["ema9"] > curr["ema21"] else "SHORT"

            log.info(
                f"{'PAPER' if self.paper else 'LIVE'} | {now_et.strftime('%H:%M')} "
                f"MGC {curr['close']:.2f} "
                f"ADX {curr['adx']:.1f} [{regime}] "
                f"EMA{cfg.EMA_FAST} {curr['ema9']:.4f} EMA{cfg.EMA_SLOW} {curr['ema21']:.4f} "
                f"cross={'YES:' + cross_dir if ema_cross else 'no'} "
                f"pos={'YES' if in_pos else 'no'} trades={self.state.trades_taken} "
                f"status={'in_position' if in_pos else self._last_skip}"
            )

            # ── Cross-down exit ──────────────────────────────────────────────
            if in_pos and ema_cross and cross_dir == "SHORT":
                if self._cross_exit_sent:
                    # Already sent a market close on a previous bar — wait for it
                    # to fill. Without this guard, a second sell can over-flatten
                    # into a naked short (incident: Celeri 2026-05-07).
                    log.info(f"  MGC cross-down repeat — exit already pending, skipping")
                    self._last_skip = "cross_exit_pending"
                    self._publish_state(adx=float(curr["adx"]),
                                        current_price=float(curr["close"]))
                    return

                log.warning(f"  MGC EMA CROSS-DOWN — closing position")
                self._cancel_protectives_for(contract, "CROSS_EXIT")
                pos = get_position_size(self.ib, contract)
                if pos != 0:
                    self._market_close(contract, int(pos), "CROSS_EXIT")
                    self._cross_exit_sent = True
                self._last_skip = "cross_exit_pending"
                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))
                return

            if in_pos:
                self._last_skip = "in_position"
                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))
                return

            # ── Signal generation ────────────────────────────────────────────
            signal = generate_signal(prev, curr, self.state, cfg, instrument="MGC")
            t_signal = time.time()

            if signal is None:
                session_open_bars = getattr(cfg, "SESSION_OPEN_BARS", 1)
                if self.state.session_bars < session_open_bars:
                    self._last_skip = "too_early"
                elif regime == "RANGE":
                    self._last_skip = f"ranging (ADX {curr['adx']:.1f})"
                else:
                    self._last_skip = "no_cross"
                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))
                return

            allowed_hours = getattr(cfg, "ALLOWED_HOURS", None)
            if allowed_hours is not None and now_et.hour not in allowed_hours:
                self._last_skip = f"hour_blocked ({now_et.hour})"
                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))
                return

            if has_open_position(self.ib, contract):
                log.warning(f"Signal on MGC but IBKR shows open position — skipping")
                self.state.in_position = True
                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))
                return

            # ── Compute size and place orders ────────────────────────────────
            entry = _round_tick(signal.entry_price)
            stop  = _round_tick(signal.stop_price)
            stop_points = abs(signal.entry_price - signal.stop_price)

            nlv = 0.0
            try:
                for v in self._acct_values():
                    if v.tag == "NetLiquidation" and v.currency == "USD":
                        nlv = float(v.value)
                        break
            except Exception:
                pass

            qty = self._compute_position_size(nlv, stop_points)
            if qty < 1:
                # Below minimum NLV threshold for safe deployment
                self._last_skip = f"insufficient_capital (NLV=${nlv:,.0f})"
                log.warning(
                    f"  MGC signal SKIPPED — insufficient capital "
                    f"(NLV=${nlv:,.0f}, MGC tier requires ≥$7,500)"
                )
                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))
                return

            log.info(
                f"\n  {'PAPER' if self.paper else 'LIVE'} SIGNAL MGC ─────────────────────\n"
                f"  MGC  LONG  via {signal.reason}\n"
                f"  Entry   {entry}\n"
                f"  Stop    {stop}  ({stop_points:.2f} pts risk)\n"
                f"  Qty     {qty}  (NLV={nlv:.0f})\n"
                f"  ─────────────────────────────────────────"
            )

            alert_signal(
                "MGC", "LONG", signal.reason,
                signal.entry_price, signal.stop_price, None,
                None, self.paper, qty=qty
            )

            self._trade_qty        = qty
            self._active_contract  = contract
            self._active_signal    = signal
            self._entry_fill_price = None
            self._cooldown         = True

            try:
                t_before_order = time.time()
                entry_trade, stop_trade = self._place_entry_with_stop(
                    contract, qty, stop
                )
                t_order_sent = time.time()

                self.state.trades_taken += 1
                self._last_skip         = "pending_fill"

                lat_bar_to_signal  = (t_signal - t_bar_received) * 1000
                lat_signal_to_prep = (t_before_order - t_signal) * 1000
                lat_prep_to_sent   = (t_order_sent - t_before_order) * 1000
                lat_bar_to_sent    = (t_order_sent - t_bar_received) * 1000

                log.info(
                    f"  LATENCY — bar→signal: {lat_bar_to_signal:.0f}ms  |  "
                    f"signal→prep: {lat_signal_to_prep:.0f}ms  |  "
                    f"prep→sent: {lat_prep_to_sent:.0f}ms  |  "
                    f"bar→sent total: {lat_bar_to_sent:.0f}ms"
                )

                log.info(
                    f"  Orders placed — MGC LONG x{qty}\n"
                    f"  Entry=MARKET  Stop={stop}\n"
                    f"  Stop will activate after entry fills."
                )

                @entry_trade.filledEvent
                def _on_fill(trade, fill):
                    t_fill = time.time()
                    lat_order_to_fill = (t_fill - t_order_sent) * 1000
                    lat_bar_to_fill   = (t_fill - t_bar_received) * 1000

                    self.state.in_position = True
                    self._entry_fill_price = fill.execution.price
                    self._entry_time       = time.time()
                    self._entry_dt_et      = datetime.now(ET)
                    self._last_skip        = "in_position"

                    metrics = {
                        "bar_to_signal_ms":        round(lat_bar_to_signal, 1),
                        "signal_to_order_prep_ms": round(lat_signal_to_prep, 1),
                        "order_prep_to_sent_ms":   round(lat_prep_to_sent, 1),
                        "order_to_fill_ms":        round(lat_order_to_fill, 1),
                        "bar_to_fill_total_ms":    round(lat_bar_to_fill, 1),
                    }
                    self._last_latency = metrics
                    self._log_latency(metrics)

                    log.info(
                        f"  FILLED — MGC LONG {fill.execution.shares}x @ {fill.execution.price}  "
                        f"|  order→fill: {lat_order_to_fill:.0f}ms  |  "
                        f"bar→fill TOTAL: {lat_bar_to_fill:.0f}ms"
                    )
                    alert_fill("MGC", "LONG", fill.execution.price, stop, None,
                               self.paper, qty=qty)

                @entry_trade.cancelledEvent
                def _on_cancel(trade):
                    log.warning(f"  Entry CANCELLED (MGC) — resetting")

                    try:
                        self.ib.cancelOrder(stop_trade.order)
                        log.warning(
                            f"  CANCELLED orphan stop order {stop_trade.order.orderId} "
                            f"(parent cancelled) — MGC"
                        )
                    except Exception as e:
                        log.error(
                            f"  FAILED to cancel orphan stop {stop_trade.order.orderId} "
                            f"after entry cancellation: {e}",
                            exc_info=True
                        )

                    from live.alerts import alert_order_rejected
                    alert_order_rejected(
                        "MGC", "Order cancelled — check margin/funds.", self.paper
                    )
                    self.state.in_position  = False
                    self.state.trades_taken = max(0, self.state.trades_taken - 1)
                    self._active_contract   = None
                    self._active_signal     = None
                    self._entry_fill_price  = None
                    self._entry_time        = None
                    self._entry_dt_et       = None
                    self._cooldown          = False
                    self._last_skip         = "cancelled"

                parent_id = entry_trade.order.orderId

                def _on_error(reqId, errorCode, errorString, advancedError=""):
                    if reqId != parent_id:
                        return
                    if errorCode in (201, 203, 399, 2102):
                        log.warning(
                            f"  Entry REJECTED (MGC) code={errorCode} — "
                            f"{errorString[:120]}"
                        )

                        try:
                            self.ib.cancelOrder(stop_trade.order)
                            log.warning(
                                f"  CANCELLED orphan stop order {stop_trade.order.orderId} "
                                f"(parent rejected) — MGC"
                            )
                        except Exception as e:
                            log.error(
                                f"  FAILED to cancel orphan stop {stop_trade.order.orderId} "
                                f"after entry rejection: {e}",
                                exc_info=True
                            )

                        from live.alerts import alert_order_rejected
                        alert_order_rejected(
                            "MGC", errorString[:200], self.paper, code=errorCode
                        )
                        self.state.in_position  = False
                        self.state.trades_taken = max(0, self.state.trades_taken - 1)
                        self._active_contract   = None
                        self._active_signal     = None
                        self._entry_fill_price  = None
                        self._entry_time        = None
                        self._entry_dt_et       = None
                        self._cooldown          = False
                        self._last_skip         = "rejected"
                        self.ib.errorEvent -= _on_error

                self.ib.errorEvent += _on_error

                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))

            except Exception as e:
                log.error(f"Order placement failed (MGC): {e}", exc_info=True)
                self.state.in_position = False
                self._active_contract  = None
                self._active_signal    = None
                self._entry_time       = None
                self._entry_dt_et      = None
                self._cooldown         = False
                self._last_skip        = "no_cross"
                self._publish_state(adx=float(curr["adx"]),
                                    current_price=float(curr["close"]))

    # ── Position sizing (MGC tier table + risk %) ────────────────────────────

    def _compute_position_size(self, nlv: float, stop_points: float) -> int:
        """
        Two independent caps, the SMALLER wins:

          1. Risk-percentage cap (from cfg.RISK_TIERS): don't risk more
             than X% of NLV per trade.
          2. MGC contract tier cap (from cfg.MGC_CONTRACT_TIERS): don't
             hold more contracts than account size safely supports given
             $5,074/contract overnight margin at IBKR.

        Returns 0 if NLV is below the smallest tier threshold ($7,500 default).
        Returns clamped to [1, cfg.MAX_CONTRACTS] otherwise.
        """
        if nlv <= 0 or stop_points <= 0:
            return 0

        # Cap 1: risk percentage
        risk_pct = next(
            (pct for threshold, pct in sorted(cfg.RISK_TIERS, reverse=True)
             if nlv >= threshold),
            0.005
        )
        qty_by_risk = int(nlv * risk_pct / (stop_points * MGC_POINT_VALUE))

        # Cap 2: MGC contract tier
        tier_max = 0
        contract_tiers = getattr(cfg, "MGC_CONTRACT_TIERS", None)
        if contract_tiers is not None:
            for threshold, max_c in sorted(contract_tiers, reverse=True):
                if nlv >= threshold:
                    tier_max = max_c
                    break
        else:
            tier_max = cfg.MAX_CONTRACTS  # fallback

        # Take the smaller of the two caps
        qty = min(qty_by_risk, tier_max)
        # Clamp to global ceiling
        qty = min(qty, cfg.MAX_CONTRACTS)

        # Below tier minimum returns 0 (don't trade); otherwise at least 1
        if qty < 1:
            return 0
        return qty

    # ── Entry + Stop bracket (2-legged) ──────────────────────────────────────

    def _place_entry_with_stop(self, contract, qty, stop):
        """
        Place a market entry with linked STP LMT child stop.

        MGC specifics:
          - tick size = 0.10 (Celeri MES = 0.25)
          - slip buffer = 0.40 (Celeri MES = 2.0, MNQ = 4.0). MGC's tick is
            small so we use a 4-tick buffer ($4/contract) between stop trigger
            and limit price. Tight enough to fill in normal markets, wide
            enough to fill through ordinary noise.
          - exchange = COMEX
        """
        stop = round(stop, 2)
        stop_limit_price = round(stop - MGC_SLIP_BUF, 2)

        contract.exchange = MGC_EXCHANGE

        parent = self.ib.client.getReqId()
        child  = self.ib.client.getReqId()

        entry_order = Order(
            orderId=parent, action="BUY",
            orderType="MKT", totalQuantity=qty,
            transmit=False, tif="DAY",
            outsideRth=True,
            account=self.account_id,
        )

        stop_order = Order(
            orderId=child, action="SELL",
            orderType="STP LMT", totalQuantity=qty,
            auxPrice=stop,
            lmtPrice=stop_limit_price,
            parentId=parent,
            transmit=True,
            tif="GTC",
            outsideRth=True,
            account=self.account_id,
        )

        entry_trade = self.ib.placeOrder(contract, entry_order)
        stop_trade  = self.ib.placeOrder(contract, stop_order)

        log.info(
            f"  Order IDs: entry={parent} stop={child}  "
            f"stop_trigger={stop} stop_limit={stop_limit_price}"
        )
        return entry_trade, stop_trade

    # ── Trade CSV log ────────────────────────────────────────────────────────

    def _ensure_csv(self):
        """
        Ensure trades_escaflowne.csv exists with the current schema.

        If the file exists with an OLD schema, upgrade in-place: old rows get
        empty values for new columns. Dashboard falls back to PT-converted
        time_et for those.
        """
        if not os.path.exists(self._csv_path):
            with open(self._csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADER)
            log.info(f"Trade log created: {self._csv_path}")
            return

        try:
            with open(self._csv_path, "r", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, [])
            if "entry_time_et" not in header:
                log.warning(
                    "trades_escaflowne.csv has old schema — upgrading to add "
                    "entry_time_et / exit_time_et columns."
                )
                self._upgrade_csv_header()
        except Exception as e:
            log.warning(f"Could not check CSV schema: {e}")

    def _upgrade_csv_header(self):
        """Add new columns to existing CSV header without losing old rows."""
        tmp_path = self._csv_path + ".tmp"
        try:
            with open(self._csv_path, "r", newline="") as fin, \
                 open(tmp_path, "w", newline="") as fout:
                reader = csv.reader(fin)
                writer = csv.writer(fout)

                old_header = next(reader, [])
                writer.writerow(CSV_HEADER)

                old_idx = {col: i for i, col in enumerate(old_header)}

                for row in reader:
                    new_row = []
                    for col in CSV_HEADER:
                        if col in old_idx and old_idx[col] < len(row):
                            new_row.append(row[old_idx[col]])
                        else:
                            new_row.append("")
                    writer.writerow(new_row)

            os.replace(tmp_path, self._csv_path)
            log.info(
                "Upgraded trades_escaflowne.csv schema to include "
                "entry_time_et/exit_time_et"
            )
        except Exception as e:
            log.error(f"CSV header upgrade failed: {e}", exc_info=True)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def _log_trade(self, signal, entry_price, exit_price, qty, result, pnl_dollars):
        """
        Append a trade row to trades_escaflowne.csv with proper ET timestamps.

        date          = entry date in ET (YYYY-MM-DD)
        time_et       = legacy column = exit time HH:MM:SS in ET (backcompat)
        entry_time_et = ISO ET timestamp of entry fill (the truthful one)
        exit_time_et  = ISO ET timestamp of exit
        """
        now_et      = datetime.now(ET)
        entry_dt_et = self._entry_dt_et if self._entry_dt_et else now_et
        pnl_points  = exit_price - entry_price

        with open(self._csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                entry_dt_et.strftime("%Y-%m-%d"),       # entry date in ET
                now_et.strftime("%H:%M:%S"),            # legacy exit HH:MM:SS
                entry_dt_et.isoformat(),                # entry_time_et (ISO ET)
                now_et.isoformat(),                     # exit_time_et (ISO ET)
                "MGC",                                  # instrument
                signal.reason,
                entry_price,
                exit_price,
                signal.stop_price,
                qty,
                round(pnl_points, 2),
                round(pnl_dollars, 2),
                result,
                "PAPER" if self.paper else "LIVE",
            ])

    # ── Session management ───────────────────────────────────────────────────

    def _reset_session(self, now_et):
        if self.state.trades_taken > 0:
            nlv = 0.0
            try:
                for v in self._acct_values():
                    if v.tag == "NetLiquidation" and v.currency == "USD":
                        nlv = float(v.value)
                        break
            except Exception:
                pass
            alert_daily_summary(
                self._daily_pnl, self.state.trades_taken,
                self._daily_wins, self._daily_losses, self.paper,
                nlv=nlv
            )

        log.info(f"-- New session starting {now_et.strftime('%Y-%m-%d')} --")

        was_killed  = self._killed
        kill_reason = self._kill_reason

        self.state              = SessionState()
        self._daily_pnl         = 0.0
        self._daily_wins        = 0
        self._daily_losses      = 0
        self._cross_exit_sent   = False
        self._realized_pnl_base = self._snapshot_realized_pnl()
        self._daily_loss_limit  = self._calc_loss_limit()

        if was_killed:
            positions = [pos for pos in self.ib.positions() if pos.position != 0]
            if positions:
                log.warning(
                    f"Session reset but STAYING KILLED — open positions still exist"
                )
                self._killed      = True
                self._kill_reason = kill_reason
            else:
                log.info(f"Session reset — clearing kill (no open positions)")
                self._killed      = False
                self._kill_reason = ""
        else:
            self._killed      = False
            self._kill_reason = ""

    def emergency_stop(self):
        log.warning("EMERGENCY STOP — cancelling all orders")
        cancel_all_orders(self.ib)
        self.state.in_position = False
        self._active_contract  = None
        self._active_signal    = None
        self._cross_exit_sent  = False