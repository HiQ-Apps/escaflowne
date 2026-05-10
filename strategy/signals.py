"""
strategy/signals.py — Escaflowne (Celeri-on-MGC)

VERBATIM COPY of Celeri's signals.py, with two small changes:
  1. Default instrument is "MGC" instead of "MES"
  2. Looks up MIN_STOP_POINTS_MGC instead of MIN_STOP_POINTS_MES/MNQ

The signal logic (entry crossover, ADX threshold, ATR stop sizing,
session filters) is unchanged. This is intentional — Phase 0a tests
whether Celeri's edge transfers to gold without any tuning.
"""

import logging
from dataclasses import dataclass
from typing import Literal

import pandas as pd


log = logging.getLogger("escaflowne.signals")


# ---------------------------------------------------------------------------
# Data structures (IDENTICAL to Celeri)
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    direction:    Literal["LONG"]
    entry_price:  float
    stop_price:   float
    reason:       str

    @property
    def risk(self):
        return abs(self.entry_price - self.stop_price)


@dataclass
class SessionState:
    trades_taken:     int   = 0
    session_bars:     int   = 0
    in_position:      bool  = False
    daily_pnl:        float = 0.0
    daily_loss_limit: float = -200.0


# ---------------------------------------------------------------------------
# Internal helpers (IDENTICAL to Celeri)
# ---------------------------------------------------------------------------

def _passes_session_filters(state, cfg) -> tuple[bool, str]:
    if state.in_position:
        return False, "in_position"
    if state.session_bars < cfg.SESSION_OPEN_BARS:
        return False, f"too_early (session_bars={state.session_bars} < {cfg.SESSION_OPEN_BARS})"
    if state.trades_taken >= cfg.MAX_TRADES_SESSION:
        return False, f"max_trades_reached ({state.trades_taken}/{cfg.MAX_TRADES_SESSION})"
    if state.daily_pnl <= state.daily_loss_limit:
        return False, f"daily_loss_limit (pnl={state.daily_pnl:.0f} limit={state.daily_loss_limit:.0f})"
    return True, ""


def _indicators_ready(bar) -> tuple[bool, str]:
    required = ["ema9", "ema21", "adx", "atr"]
    missing = [c for c in required if pd.isna(bar.get(c))]
    if missing:
        return False, f"nan_indicators: {missing}"
    return True, ""


def _debug_enabled(cfg) -> bool:
    return getattr(cfg, "SIGNAL_DEBUG", False)


def _no_signal(reason: str, cfg, prev=None, curr=None) -> None:
    if _debug_enabled(cfg):
        extra = ""
        if prev is not None and curr is not None:
            try:
                extra = (
                    f"  | prev: ema9={prev['ema9']:.4f} ema21={prev['ema21']:.4f} "
                    f"adx={prev['adx']:.2f} atr={prev['atr']:.2f}"
                    f"  | curr: ema9={curr['ema9']:.4f} ema21={curr['ema21']:.4f} "
                    f"adx={curr['adx']:.2f} atr={curr['atr']:.2f} close={curr['close']:.2f}"
                )
            except (KeyError, TypeError):
                pass
        log.info(f"NO SIGNAL — {reason}{extra}")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_signal(prev, curr, state, cfg, instrument="MGC") -> Signal | None:
    """
    Generate a LONG entry signal when EMA9 crosses above EMA21.

    LOGIC IS IDENTICAL to Celeri. Only the min-stop lookup differs:
    we read MIN_STOP_POINTS_MGC from the config instead of MES/MNQ values.
    """
    # 1. Session filters
    passed, reason = _passes_session_filters(state, cfg)
    if not passed:
        return _no_signal(f"session_filter: {reason}", cfg, prev, curr)

    # 2. Indicator readiness
    ready, reason = _indicators_ready(curr)
    if not ready:
        return _no_signal(f"curr {reason}", cfg, prev, curr)
    ready, reason = _indicators_ready(prev)
    if not ready:
        return _no_signal(f"prev {reason}", cfg, prev, curr)

    # 3. ADX threshold
    if curr["adx"] < cfg.ADX_TREND_THRESHOLD:
        return _no_signal(
            f"adx_below_threshold (adx={curr['adx']:.2f} < {cfg.ADX_TREND_THRESHOLD})",
            cfg, prev, curr,
        )

    # 4. Long crossover
    curr_bull = curr["ema9"] > curr["ema21"]
    prev_bull = prev["ema9"] > prev["ema21"]
    if not (curr_bull and not prev_bull):
        if not curr_bull:
            return _no_signal(
                f"no_cross: curr ema9 ({curr['ema9']:.4f}) not above ema21 ({curr['ema21']:.4f})",
                cfg, prev, curr,
            )
        else:
            return _no_signal(
                f"no_cross: prev already bullish (prev ema9 {prev['ema9']:.4f} > prev ema21 {prev['ema21']:.4f})",
                cfg, prev, curr,
            )

    # 5. Min risk check (MGC-specific min stop)
    min_stop = getattr(cfg, "MIN_STOP_POINTS_MGC", 1.0)

    entry = curr["close"]
    stop  = entry - max(min_stop, curr["atr"] * cfg.ATR_STOP_MULT)
    risk  = entry - stop

    if risk < cfg.EMA_MIN_RISK:
        return _no_signal(
            f"risk_too_small (risk={risk:.2f} < EMA_MIN_RISK={cfg.EMA_MIN_RISK})",
            cfg, prev, curr,
        )

    return Signal(
        direction="LONG",
        entry_price=entry,
        stop_price=stop,
        reason="EMA_LONG",
    )