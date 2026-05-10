"""
config/base.py — Escaflowne (Celeri-on-MGC)

MGC = COMEX Micro Gold Futures
  - Contract size: 10 troy ounces
  - Tick size:     $0.10/oz  (= $1.00 per tick)
  - Point value:   $10/point (= $10 per $1.00/oz move)

Compare to Celeri:
  MES: $0.25 tick, $5/point   (1 tick = $1.25)
  MNQ: $0.25 tick, $2/point   (1 tick = $0.50)
  MGC: $0.10 tick, $10/point  (1 tick = $1.00)

Note: gold's "point" in trading-platform terms is $1.00 per ounce.
A move from 4000.0 to 4001.0 = 1 point = $10 per contract.
Gold typically moves 5-30 points per active 5-min bar.
"""

# ── MGC contract specs ────────────────────────────────────────────────────
COMMISSION_PER_SIDE = 0.62      # IBKR Pro commodity futures, similar to MES
SLIPPAGE_TICKS      = 1.5       # Gold's tick is $0.10, so 1.5 ticks = $0.15
TICK_SIZE           = 0.10
POINT_VALUE         = 10.00