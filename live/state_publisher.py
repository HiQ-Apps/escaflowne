"""
live/state_publisher.py — Escaflowne
Writes the bot's current state to a small JSON file so monitors can read it.

Ported from Celeri's state_publisher.py. Single change:
  - Writes to `state_escaflowne.json` instead of `state.json` so the two
    bots don't clobber each other's state files when both run on the
    same VPS.

Atomic write-then-rename prevents partial reads. Failures are silent —
state.json is non-critical so it never crashes the bot.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
import pytz

# Different filename from Celeri's state.json to avoid collision when both
# bots run on the same VPS.
STATE_PATH = Path(__file__).parent.parent / "state_escaflowne.json"
ET = pytz.timezone("US/Eastern")


def write_state(
    nlv: float = None,
    day_pnl: float = None,
    status: str = "unknown",
    in_position: bool = False,
    adx: float = None,
    current_price: float = None,
    position_pnl: float = None,
    instrument: str = None,
    trades_today: int = 0,
    paper: bool = True,
    killed: bool = False,
    kill_reason: str = "",
) -> None:
    """
    Atomically write current state to STATE_PATH.

    ts is written in ET to match ALLOWED_HOURS and backtest conventions.
    ts_epoch is Unix epoch (timezone-agnostic) for "minutes ago" calculations.
    """
    now_et    = datetime.now(ET)
    now_epoch = now_et.timestamp()

    data = {
        "ts":             now_et.strftime("%Y-%m-%dT%H:%M:%S"),
        "ts_epoch":       now_epoch,
        "bot":            "escaflowne",
        "mode":           "PAPER" if paper else "LIVE",
        "nlv":            round(nlv, 2) if nlv is not None else None,
        "day_pnl":        round(day_pnl, 2) if day_pnl is not None else None,
        "status":         status,
        "in_position":    bool(in_position),
        "adx":            round(adx, 2) if adx is not None else None,
        "current_price":  round(current_price, 2) if current_price is not None else None,
        "position_pnl":   round(position_pnl, 2) if position_pnl is not None else None,
        "instrument":     instrument,
        "trades_today":   trades_today,
        "killed":         bool(killed),
        "kill_reason":    kill_reason or "",
    }

    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=STATE_PATH.parent, prefix=".state_esc_", suffix=".tmp"
        )
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, STATE_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        # Silent — state is non-critical