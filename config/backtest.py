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

# ── MGC contract tiers ───────────────────────────────────────────────────────
# Maps NLV -> max contracts allowed.
# MGC needs $5,074 overnight margin per contract at IBKR, so tiers are calibrated
# to ensure ~$2,500+ buffer per contract (Monte Carlo p5 DD = $1,837).
#
# Sized list = list of (nlv_threshold, max_contracts) sorted descending.
# Sizing function picks the largest tier with threshold <= current NLV.
MGC_CONTRACT_TIERS = [
    (500_000,  20),   # $500K+:  20 contracts
    (250_000,  15),   # $250K+:  15
    (150_000,  12),   # $150K+:  12
    (100_000,  10),   # $100K+:  10
    ( 75_000,   8),   # $75K+:   8
    ( 50_000,   6),   # $50K+:   6
    ( 35_000,   4),   # $35K+:   4
    ( 25_000,   3),   # $25K+:   3 (~$15.2K margin + ~$10K buffer)
    ( 15_000,   2),   # $15K+:   2 (~$10.1K margin + ~$5K buffer)
    (  7_500,   1),   # $7.5K+:  1 (~$5.1K margin + ~$2.5K buffer)
    (      0,   0),   # < $7.5K: don't deploy (insufficient margin buffer)
]

# Hard ceiling regardless of MGC_CONTRACT_TIERS — slippage gets nasty above this
MAX_CONTRACTS = 25

SIGNAL_DEBUG = False  # Backtest is silent by default; flip on for one-off debugging