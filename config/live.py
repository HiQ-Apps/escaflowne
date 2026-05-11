"""
config/live.py — Escaflowne live trading runtime config.

Pulls the walk-forward-optimized parameter set. Live trading should match
backtest exactly. If you change any param here, you should also change it
in config/backtest.py and re-run validation.
"""

# ── Strategy params (optimized via walk-forward, 2026-05-09) ──────────────────
ADX_TREND_THRESHOLD = 14
ADX_MUST_RISE       = False

EMA_FAST = 8
EMA_SLOW = 21

# Risk
ATR_STOP_MULT       = 4.0
MIN_STOP_POINTS_MGC = 2.0    # absolute floor on stop distance in MGC points

# Position management
MAX_CONTRACTS = 25            # hard ceiling; tiered sizing usually well below this

# Risk tiers (NLV threshold -> per-trade risk %)
RISK_TIERS = [
    (500_000, 0.010),
    (100_000, 0.012),
    ( 50_000, 0.015),
    ( 20_000, 0.018),
    ( 10_000, 0.020),
    (      0, 0.025),
]

# Contract tiers (NLV threshold -> max contracts allowed at this NLV)
# MGC needs $5,074 overnight margin per contract at IBKR. Tiers calibrated
# to ensure ~$2,500+ buffer per contract (Monte Carlo p5 DD = $1,837).
MGC_CONTRACT_TIERS = [
    (500_000,  20),
    (250_000,  15),
    (150_000,  12),
    (100_000,  10),
    ( 75_000,   8),
    ( 50_000,   6),
    ( 35_000,   4),
    ( 25_000,   3),
    ( 15_000,   2),
    (  7_500,   1),
    (      0,   0),   # below $7.5K: do not deploy
]

# ── Operational ──────────────────────────────────────────────────────────────
INSTRUMENT  = "MGC"
POINT_VALUE = 10.0        # $10/point for MGC (vs $5 for MES, $2 for MNQ)
TICK_SIZE   = 0.10        # $0.10 per tick for MGC
EMA_MIN_RISK = 0.5
# Session
ENABLE_EOD_EXIT = "friday_only"   # flat-by-Fri-close, hold overnight Mon-Thu

# Trade cooldown
COOLDOWN_BARS_AFTER_STOP = 0      # no cooldown by default
SESSION_OPEN_BARS = 1

# ── IBKR connection ──────────────────────────────────────────────────────────
PAPER       = True                # set to False when going live
PORT_PAPER  = 4002
PORT_LIVE   = 4001
MAX_TRADES_SESSION = 10
# Discord webhook is read from .env (DISCORD_WEBHOOK_URL, DISCORD_USER_ID)