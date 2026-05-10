"""
config/backtest.py — Escaflowne (Celeri-on-MGC)

Phase 0a: Celeri's EXACT strategy parameters, retuned only for MGC's price scale.

The strategy params (ADX threshold, EMA periods, ATR multiplier) are held identical
to Celeri's live config. Only scale-dependent values are recalibrated for gold.

If this exact configuration produces edge on MGC, the EMA-cross+ADX edge transfers
across instruments. If it doesn't, we'll know whether the edge is fragile (specific
to equity index futures) before we sweep.
"""

from config.base import *

# ── Backtest setup ────────────────────────────────────────────────────────
STARTING_CAPITAL = 5_000.0

# ── Regime detection (IDENTICAL to Celeri live) ───────────────────────────
ADX_TREND_THRESHOLD = 14

# ── Entry & stop sizing ───────────────────────────────────────────────────
# Strategy params: IDENTICAL to Celeri
ATR_STOP_MULT = 3.0

# Scale-dependent params: recalibrated for gold's typical 5-min ATR
# Celeri MES: MIN_STOP_POINTS=4.0 means $20 minimum risk per contract
# Celeri MNQ: MIN_STOP_POINTS=6.0 means $12 minimum risk per contract
# MGC seed: 1.0 point = $10 minimum risk per contract — slightly tighter
#           than Celeri's MES floor in dollar terms; we'll inspect actual ATR
#           after data is loaded and tune if obviously wrong.
MIN_STOP_POINTS_MGC = 1.0

# Celeri EMA_MIN_RISK=2.5 on MES = $12.50 minimum stop dist per contract.
# Same idea for MGC: 0.5 points = $5 minimum stop dist per contract.
# Gold rarely produces stops this tight after the ATR floor, so this is mostly
# a backstop against degenerate signals.
EMA_MIN_RISK = 0.5

# ── Session filters (IDENTICAL to Celeri live) ────────────────────────────
SESSION_OPEN_BARS  = 1
MAX_TRADES_SESSION = 10
ALLOWED_HOURS      = None

# ── Exit behavior ─────────────────────────────────────────────────────────
# Gold has real weekend gap risk too (Sunday Asian session can gap on news)
ENABLE_EOD_EXIT = "friday_only"

# ── Risk (IDENTICAL to Celeri live) ───────────────────────────────────────
DAILY_LOSS_PCT = 0.04

RISK_TIERS = [
    (500_000, 0.010),
    (100_000, 0.012),
    ( 50_000, 0.015),
    ( 20_000, 0.018),
    ( 10_000, 0.020),
    (      0, 0.025),
]

MAX_CONTRACTS = 1
SIGNAL_DEBUG = False  # Backtest is silent by default; flip on for one-off debugging