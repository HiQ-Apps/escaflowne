"""
gui/menubar.py — Escaflowne
macOS menu bar app. Apple-minimal aesthetic. NO Dock icon.

Shows in menu bar:
  🟢 FLAT $229,898
  🟡 POS $229,920 +$22
  🔴 HALTED $229,800 -$98

Click for detail dropdown.

Differences from Celeri's menubar:
  - Hides Dock icon at startup (NSApplicationActivationPolicyAccessory)
  - Reads state_escaflowne.json instead of state.json
  - 🜚 alchemical gold symbol instead of ◆ diamond
  - Brand: "Escaflowne" instead of "Celeri"

Install:
  pip install rumps pyobjc-framework-Cocoa

Run:
  python -m gui.menubar
"""

# ─────────────────────────────────────────────────────────────────────────────
# Hide Dock icon FIRST, before rumps imports — must happen before NSApplication
# is initialized. NSApplicationActivationPolicyAccessory = background-only app
# (no Dock icon, no Cmd-Tab entry, but menu bar items still work).
# ─────────────────────────────────────────────────────────────────────────────
try:
    import AppKit
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )
except ImportError:
    print("Install pyobjc-framework-Cocoa for Dock icon hiding:")
    print("  pip install pyobjc-framework-Cocoa")
    # Continue anyway — app still works, Dock icon will just be visible.

import json
import time
from pathlib import Path

try:
    import rumps
except ImportError:
    print("Install rumps first:  pip install rumps")
    raise SystemExit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default state file location — assumes menubar runs from project root or
# adjacent to the bot. Override via ESCAFLOWNE_STATE_PATH env var if you sync
# the state.json to a different location on the Mac.
import os
_default_state = Path(__file__).parent.parent / "state_escaflowne.json"
STATE_PATH = Path(os.getenv("ESCAFLOWNE_STATE_PATH", _default_state))

POLL_INTERVAL = 1.0    # seconds — how often to re-read state.json
STALE_SECONDS = 300    # seconds — show STALE warning if state hasn't updated


# ─────────────────────────────────────────────────────────────────────────────
# Title formatting
# ─────────────────────────────────────────────────────────────────────────────

def _dollar(val):
    if val is None:
        return "—"
    if abs(val) >= 1000:
        return f"${val/1000:.1f}k" if abs(val) < 10000 else f"${val:,.0f}"
    return f"${val:,.0f}"


def _signed_dollar(val):
    if val is None or val == 0:
        return ""
    sign = "+" if val >= 0 else "-"
    return f"  {sign}{_dollar(abs(val))}"


def _title(state):
    if state is None:
        return "🜚 Escaflowne"

    if state.get("killed"):
        dot, status = "🔴", "HALTED"
    elif state.get("in_position"):
        dot, status = "🟡", "POS"
    else:
        status_raw = (state.get("status") or "").lower()
        if "pending" in status_raw:
            dot, status = "🟡", "PENDING"
        elif "cooldown" in status_raw:
            dot, status = "🟢", "COOLDOWN"
        else:
            dot, status = "🟢", "FLAT"

    nlv = state.get("nlv")
    day = state.get("day_pnl")

    extra = ""
    if state.get("in_position") and state.get("position_pnl") is not None:
        extra = _signed_dollar(state["position_pnl"])
    elif day:
        extra = _signed_dollar(day)

    return f"{dot} {status} {_dollar(nlv)}{extra}"


def _fmt_age(seconds):
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds//60)}m ago"
    return f"{seconds/3600:.1f}h ago"


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

class EscaflowneMenuBar(rumps.App):
    def __init__(self):
        super().__init__("Escaflowne", title="🜚 Escaflowne", quit_button=None)

        self.item_mode      = rumps.MenuItem("—")
        self.item_updated   = rumps.MenuItem("—")
        self.item_nlv       = rumps.MenuItem("NLV: —")
        self.item_day       = rumps.MenuItem("Day P&L: —")
        self.item_pos       = rumps.MenuItem("Position: —")
        self.item_pos_pnl   = rumps.MenuItem("Unrealized: —")
        self.item_inst      = rumps.MenuItem("Instrument: —")
        self.item_price     = rumps.MenuItem("Price: —")
        self.item_adx       = rumps.MenuItem("ADX: —")
        self.item_status    = rumps.MenuItem("Status: —")
        self.item_trades    = rumps.MenuItem("Trades today: —")
        self.quit_item      = rumps.MenuItem("Quit", callback=rumps.quit_application)

        self.menu = [
            self.item_mode,
            self.item_updated,
            None,
            self.item_nlv,
            self.item_day,
            None,
            self.item_pos,
            self.item_pos_pnl,
            None,
            self.item_inst,
            self.item_price,
            self.item_adx,
            None,
            self.item_status,
            self.item_trades,
            None,
            self.quit_item,
        ]

    @rumps.timer(POLL_INTERVAL)
    def refresh(self, _):
        state = self._load_state()
        self.title = _title(state)

        if state is None:
            self.item_mode.title    = "No state_escaflowne.json found"
            self.item_updated.title = f"Path: {STATE_PATH}"
            return

        try:
            age = time.time() - state.get("ts_epoch", 0)
            if age > STALE_SECONDS:
                self.title = f"⚠ STALE  {_fmt_age(age)}"
        except Exception:
            age = None

        self.item_mode.title = f"Mode: {state.get('mode', '—')}"
        if age is not None:
            self.item_updated.title = f"Updated: {_fmt_age(age)}"
        else:
            self.item_updated.title = f"Updated: {state.get('ts', '—')}"

        nlv = state.get("nlv")
        self.item_nlv.title = f"NLV: ${nlv:,.2f}" if nlv is not None else "NLV: —"

        day = state.get("day_pnl")
        if day is not None:
            sign = "+" if day >= 0 else ""
            self.item_day.title = f"Day P&L: {sign}${day:,.2f}"
        else:
            self.item_day.title = "Day P&L: —"

        in_pos = state.get("in_position", False)
        self.item_pos.title = "Position: IN TRADE" if in_pos else "Position: flat"

        pos_pnl = state.get("position_pnl")
        if in_pos and pos_pnl is not None:
            sign = "+" if pos_pnl >= 0 else ""
            self.item_pos_pnl.title = f"Unrealized: {sign}${pos_pnl:,.2f}"
        else:
            self.item_pos_pnl.title = "Unrealized: —"

        self.item_inst.title = f"Instrument: {state.get('instrument') or 'MGC'}"

        px = state.get("current_price")
        self.item_price.title = f"Price: {px:,.2f}" if px is not None else "Price: —"

        adx = state.get("adx")
        self.item_adx.title = f"ADX: {adx:.1f}" if adx is not None else "ADX: —"

        if state.get("killed"):
            status = f"HALTED: {state.get('kill_reason', 'unknown')}"
        else:
            status = state.get("status", "—")
        self.item_status.title = f"Status: {status}"

        self.item_trades.title = f"Trades today: {state.get('trades_today', 0)}"

    def _load_state(self):
        try:
            if not STATE_PATH.exists():
                return None
            with open(STATE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return None


def main():
    """Entry point for `escaflowne-menubar` command."""
    EscaflowneMenuBar().run()


if __name__ == "__main__":
    main()