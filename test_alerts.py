"""
test_alerts.py v4 — MOBILE-FIRST Escaflowne alerts.

Lessons from v3 (which looked bad on mobile):
  - Code blocks render with huge padding on mobile — DON'T use
  - Long ━/─ characters wrap on narrow screens — DON'T use
  - `diff` syntax shows literal + and - prefixes on mobile — DON'T use
  - ### and ## markdown headers add big vertical gaps — DON'T use
  - Inline fields stack vertically on mobile — DON'T use

What DOES work on mobile:
  - The colored left bar from embed.color (carries signal type)
  - Embed title (bold, single line)
  - Embed description with simple markdown (bold, inline code)
  - Emoji as visual punctuation

Goal: every alert is 2-3 lines max on mobile. Let the color bar do the heavy lifting.

Run on Mac:
    python test_alerts.py
"""

import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_TEST") or os.getenv("DISCORD_WEBHOOK_URL")

if not WEBHOOK_URL:
    print("Set DISCORD_WEBHOOK_TEST or DISCORD_WEBHOOK_URL in .env first.")
    raise SystemExit(1)

BOT_NAME = "ESCAFLOWNE"


# ── Colors (left bar on the embed) ─────────────────────────────────────────
COLOR_WIN     = 0xFFD700   # bright gold
COLOR_LOSS    = 0xFF3A4A   # red
COLOR_SIGNAL  = 0xC9A35A   # tarnished gold
COLOR_FILL    = 0xFFB020   # amber


def _post(payload, label):
    print(f"  Posting: {label}")
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code not in (200, 204):
            print(f"    WARN: {resp.status_code}  {resp.text[:200]}")
    except Exception as e:
        print(f"    ERR: {e}")
    time.sleep(1.5)


# ════════════════════════════════════════════════════════════════════════════
# Version A — Title-only, minimal embed
# ════════════════════════════════════════════════════════════════════════════
# Just a title with all info inline. No description. Color bar = signal type.

def variant_a_entry():
    _post({"embeds": [{
        "color": COLOR_SIGNAL,
        "title": "🟢  ENTRY · MGC ×7 @ 4,710.50",
        "description": "Stop `4,683.50` · Risk 27.00 pts · `EMA_LONG_CROSS`",
    }]}, "A — ENTRY")


def variant_a_filled():
    _post({"embeds": [{
        "color": COLOR_FILL,
        "title": "✅  FILLED · MGC ×7 @ 4,710.50",
        "description": "🔴 Stop `4,683.50` · Exit on EMA cross-down",
    }]}, "A — FILLED")


def variant_a_win():
    _post({"embeds": [{
        "color": COLOR_WIN,
        "title": "💰  WIN · +$320.00",
        "description": "MGC ×7 · `4,710.50 → 4,715.20` · 14 min\nDay P&L **+$320.00** · NLV $230,218",
    }]}, "A — WIN")


def variant_a_loss():
    _post({"embeds": [{
        "color": COLOR_LOSS,
        "title": "🔻  LOSS · -$135.00",
        "description": "MGC ×7 · `4,710.50 → 4,693.50` · 8 min\nDay P&L **-$135.00** · NLV $229,763",
    }]}, "A — LOSS")


# ════════════════════════════════════════════════════════════════════════════
# Version B — Description-only with bold lead, no title
# ════════════════════════════════════════════════════════════════════════════
# Cleaner; no big title text. Bold first line carries the punch.

def variant_b_entry():
    desc = (
        "**🟢 ENTRY** · `MGC ×7 @ 4,710.50`\n"
        "Stop `4,683.50` · Risk 27.00 pts"
    )
    _post({"embeds": [{"color": COLOR_SIGNAL, "description": desc}]}, "B — ENTRY")


def variant_b_filled():
    desc = (
        "**✅ FILLED** · `MGC ×7 @ 4,710.50`\n"
        "🔴 Stop `4,683.50` · Exit: EMA cross-down"
    )
    _post({"embeds": [{"color": COLOR_FILL, "description": desc}]}, "B — FILLED")


def variant_b_win():
    desc = (
        "**💰 WIN · +$320.00**\n"
        "MGC ×7 · `4,710.50 → 4,715.20` · 14 min\n"
        "Day P&L **+$320.00** · NLV $230,218"
    )
    _post({"embeds": [{"color": COLOR_WIN, "description": desc}]}, "B — WIN")


def variant_b_loss():
    desc = (
        "**🔻 LOSS · -$135.00**\n"
        "MGC ×7 · `4,710.50 → 4,693.50` · 8 min\n"
        "Day P&L **-$135.00** · NLV $229,763"
    )
    _post({"embeds": [{"color": COLOR_LOSS, "description": desc}]}, "B — LOSS")


# ════════════════════════════════════════════════════════════════════════════
# Version C — Ultra-minimal: just a colored title bar, ONE line
# ════════════════════════════════════════════════════════════════════════════
# Absolute minimum. Everything that matters in one line.

def variant_c_entry():
    _post({"embeds": [{
        "color": COLOR_SIGNAL,
        "title": "🟢 ENTRY · MGC ×7 @ 4,710.50 · stop 4,683.50",
    }]}, "C — ENTRY one-liner")


def variant_c_win():
    _post({"embeds": [{
        "color": COLOR_WIN,
        "title": "💰 WIN · MGC +$320.00 · NLV $230,218",
    }]}, "C — WIN one-liner")


def variant_c_loss():
    _post({"embeds": [{
        "color": COLOR_LOSS,
        "title": "🔻 LOSS · MGC -$135.00 · NLV $229,763",
    }]}, "C — LOSS one-liner")


# ════════════════════════════════════════════════════════════════════════════
# Heartbeats
# ════════════════════════════════════════════════════════════════════════════

def heartbeat_flat():
    _post({"content": "🟢 ESCAFLOWNE · $229,898 · FLAT"}, "HB FLAT")

def heartbeat_inpos():
    _post({"content": "🟡 ESCAFLOWNE · $229,920 · IN POSITION"}, "HB IN POS")

def heartbeat_halted():
    _post({"content": "🔴 ESCAFLOWNE · $229,800 · HALTED"}, "HB HALTED")


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def main():
    print(f"Posting MOBILE-FIRST alerts to Discord...")
    print()

    _post({"content": "═══ **MOBILE TEST v4** ═══"}, "header")
    time.sleep(2)

    _post({"content": "**Version A — title + description**"}, "sep")
    variant_a_entry()
    variant_a_filled()
    variant_a_win()
    variant_a_loss()

    _post({"content": "**Version B — description only, bold lead**"}, "sep")
    variant_b_entry()
    variant_b_filled()
    variant_b_win()
    variant_b_loss()

    _post({"content": "**Version C — ultra minimal, one line**"}, "sep")
    variant_c_entry()
    variant_c_win()
    variant_c_loss()

    _post({"content": "**Heartbeats**"}, "sep")
    heartbeat_flat()
    heartbeat_inpos()
    heartbeat_halted()

    print()
    print("Done. Look at it on mobile — pick A, B, or C (or mix and match).")


if __name__ == "__main__":
    main()