"""
live/feed.py — Escaflowne
Subscribes to live 5-minute MGC bars from IBKR using keepUpToDate=True.

Ported from Celeri's feed.py. Differences:
  - EMA periods read from config.live (EMA_FAST, EMA_SLOW) instead of
    hardcoded 9/21. Escaflowne's walk-forward picked EMA(8, 21).
  - Logger renamed to escaflowne.feed
  - Same EMA cache fix to protect against IBKR bar revisions silently
    changing the prev bar's EMA values on the next update.
"""

import logging
import threading
import time
from datetime import datetime

import pandas as pd
import pandas_ta as ta
from ib_insync import IB, Future, BarDataList

import config.live as cfg

log = logging.getLogger("escaflowne.feed")

TICK_LOG_INTERVAL   = 5
FEED_TIMEOUT        = 120
FEED_CHECK_INTERVAL = 30


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute EMA fast/slow, volume MA, ATR, and ADX on the 5-min frame.

    EMA periods are pulled from config.live so a different bot/instrument
    can use different EMA pairs without changing this file. Note that the
    column names ('ema9', 'ema21') are kept stable regardless of the
    actual periods — signals.py looks for these column names, not the
    underlying periods. The number suffix is just a historical name.
    """
    df = df.copy()
    df["ema9"]      = ta.ema(df["close"], length=cfg.EMA_FAST)
    df["ema21"]     = ta.ema(df["close"], length=cfg.EMA_SLOW)
    df["volume_ma"] = ta.sma(df["volume"], length=20)
    df["atr"]       = ta.atr(df["high"], df["low"], df["close"], length=14)
    adx_df    = ta.adx(df["high"], df["low"], df["close"], length=14)
    df["adx"] = adx_df["ADX_14"]
    return df


def _ibkr_bars_to_df(bars: BarDataList) -> pd.DataFrame:
    records = [
        {
            "timestamp": pd.Timestamp(bar.date),
            "open":      bar.open,
            "high":      bar.high,
            "low":       bar.low,
            "close":     bar.close,
            "volume":    bar.volume,
        }
        for bar in bars
    ]
    df = pd.DataFrame(records).set_index("timestamp")
    if df.index.tzinfo is not None:
        df.index = df.index.tz_convert("US/Eastern")
    else:
        df.index = df.index.tz_localize("US/Eastern")
    return df


class LiveFeed:
    """
    Manages a streaming 5-min bar subscription for one contract.

    EMA caching: when a bar closes, we cache its EMA9/EMA21 values.
    On the next bar, we overwrite the recalculated "prev" EMA values
    with the cached ones, ensuring cross detection uses stable values
    rather than whatever IBKR's bar revision recomputes.
    """

    def __init__(self, ib: IB, contract: Future, on_bar, label: str = None, get_status=None):
        self.ib         = ib
        self.contract   = contract
        self.on_bar     = on_bar
        self.label      = label or contract.symbol
        self.get_status = get_status
        self._bars      = None
        self._last_tick_log  = 0
        self._last_update    = time.time()
        self._stopped        = False
        self._heartbeat_thread = None

        # ── EMA cache — prevents bar revision from corrupting cross detection ──
        self._cached_ema9  = None
        self._cached_ema21 = None
        self._cached_adx   = None
        self._cached_ts    = None   # timestamp of cached bar

    def start(self):
        log.info(f"Starting 5-min bar feed for {self.label}...")
        self._stopped = False
        self._subscribe()
        self._start_heartbeat()

    def stop(self):
        self._stopped = True
        self._unsubscribe()
        log.info(f"{self.label}: Feed stopped")

    def _subscribe(self):
        try:
            self._bars = self.ib.reqHistoricalData(
                contract=self.contract,
                endDateTime="",
                durationStr="2 D",  # Celeri found 30 D caused a 55-min post-reconnect dead window
                barSizeSetting="5 mins",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                keepUpToDate=True,
            )
            self._bars.updateEvent += self._on_bar_update
            self._last_update = time.time()
            log.info(f"{self.label}: Seeded with {len(self._bars)} historical bars")
        except Exception as e:
            log.error(f"{self.label}: Failed to subscribe: {e}", exc_info=True)

    def _unsubscribe(self):
        try:
            if self._bars:
                self.ib.cancelHistoricalData(self._bars)
                self._bars = None
        except Exception as e:
            log.warning(f"{self.label}: Error unsubscribing: {e}")

    def _start_heartbeat(self):
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        """Monitor feed health. Just logs — run.py watchdog handles reconnects."""
        while not self._stopped:
            time.sleep(FEED_CHECK_INTERVAL)
            if self._stopped:
                break
            stale = time.time() - self._last_update
            if stale > FEED_TIMEOUT:
                log.warning(
                    f"{self.label}: No updates for {stale:.0f}s "
                    f"(timeout={FEED_TIMEOUT}s) — feed appears dead, watchdog will reconnect"
                )

    def _on_bar_update(self, bars: BarDataList, has_new_bar: bool = False):
        if not bars:
            return

        self._last_update = time.time()

        current_bar = bars[-1]
        now_str     = datetime.now().strftime("%H:%M:%S")

        if not has_new_bar:
            # Intra-bar tick update
            status     = self.get_status() if self.get_status else ""
            status_str = f"  ·  {status}" if status else ""
            tick_line = (
                f"[{now_str}]  {self.label} {current_bar.close:.2f}  "
                f"hi {current_bar.high:.2f}  lo {current_bar.low:.2f}  "
                f"vol {int(current_bar.volume)}{status_str}"
            )
            print(tick_line, flush=True)
            now = time.monotonic()
            if now - self._last_tick_log >= TICK_LOG_INTERVAL:
                self._last_tick_log = now
                log.info(tick_line)
            return

        # ── Completed bar ──────────────────────────────────────────────────────
        print(flush=True)
        df = _ibkr_bars_to_df(bars)
        df = _add_indicators(df)
        df = df.dropna()

        if len(df) < 2:
            log.debug(f"{self.label}: Not enough bars yet for signal check")
            return

        # ── EMA cache fix ──────────────────────────────────────────────────────
        # IBKR sometimes revises the previous bar's OHLCV data slightly after
        # a new bar arrives, causing the full EMA recalculation to produce
        # different "prev" values than what was logged at bar close.
        # Solution: overwrite prev bar's EMA/ADX with the values we cached
        # when that bar was the current bar.
        curr_ts = df.index[-1]

        if (self._cached_ts is not None and
                self._cached_ts == df.index[-2] and
                self._cached_ema9 is not None):
            # Restore stable prev values from cache
            df.at[df.index[-2], "ema9"]  = self._cached_ema9
            df.at[df.index[-2], "ema21"] = self._cached_ema21
            df.at[df.index[-2], "adx"]   = self._cached_adx
            log.debug(
                f"{self.label}: Restored cached prev EMA9={self._cached_ema9:.4f} "
                f"EMA21={self._cached_ema21:.4f} ADX={self._cached_adx:.1f}"
            )

        # Cache current bar's values for next bar
        self._cached_ts   = curr_ts
        self._cached_ema9 = float(df.iloc[-1]["ema9"])
        self._cached_ema21= float(df.iloc[-1]["ema21"])
        self._cached_adx  = float(df.iloc[-1]["adx"])
        # ──────────────────────────────────────────────────────────────────────

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        try:
            self.on_bar(prev, curr, self.label)
        except Exception as e:
            log.error(f"Error in on_bar callback ({self.label}): {e}", exc_info=True)