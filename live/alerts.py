"""
live/alerts.py — Escaflowne
Discord webhook alerts. Mobile-first compact design.

Design principles (locked in 2026-05-11):
  - No "ESCAFLOWNE" branding inside alerts (channel name = bot identity)
  - No author line on most alerts (saves vertical space)
  - No code blocks, no ASCII banners (they break on mobile Discord)
  - WIN uses GREEN, not gold (universal "money up" color)
  - Direction-free everywhere (long-only strategy)
  - Heartbeats are plain text one-liners
  - Color bar carries the alert type (gold = entry, amber = filled,
    green = win, red = loss/danger, periwinkle = system, gray = neutral)

All public functions keep backward-compatible signatures so runner.py and
run_trade.py don't need to change. Unused params (direction, target, rr,
uptime_str, trades_today) are accepted but ignored where the new design
doesn't need them.

Configuration (in .env):
  DISCORD_WEBHOOK_URL   — channel webhook (use separate channel from Celeri)
  DISCORD_USER_ID       — your Discord user ID for @mentions on alerts
                          that ping you (kill switch, unprotected, rejected)
"""

import logging
import os
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("escaflowne.alerts")


# ── Bot identity ──────────────────────────────────────────────────────────────
# Kept here for restart/shutdown messages that DO want to identify which bot,
# in case you ever consolidate channels later. Inline alerts don't use it.
BOT_NAME    = "ESCAFLOWNE"
BOT_DIAMOND = "🜚"
INSTRUMENT  = "MGC"


# ── Colors (the left bar on embeds) ───────────────────────────────────────────
COLOR_WIN     = 0x2ECC71  # GREEN — money up
COLOR_LOSS    = 0xFF3A4A  # red — money down
COLOR_SIGNAL  = 0xC9A35A  # tarnished gold — entry signal
COLOR_FILL    = 0xFFB020  # amber — order filled
COLOR_SYSTEM  = 0x7C83FD  # periwinkle — system messages
COLOR_NEUTRAL = 0x4A5568  # dark gray — info, shutdown
COLOR_DANGER  = 0xFF3A4A  # red — kill switch, unprotected, errors


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_webhook_url():
    return os.getenv("DISCORD_WEBHOOK_URL", "")

def _get_user_id():
    return os.getenv("DISCORD_USER_ID", "")

def _tick_round(price: float, tick: float = 0.10) -> str:
    """MGC tick = $0.10."""
    if price is None:
        return "—"
    rounded = round(round(price / tick) * tick, 2)
    return f"{rounded:.2f}"

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _fmt_date() -> str:
    now = datetime.now()
    return f"{now.strftime('%a %b')} {now.day} {now.year}"

def _fmt_time() -> str:
    now = datetime.now()
    hour = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    return f"{hour}:{now.strftime('%M')} {ampm}"

def _send_embed(embed: dict, mention: bool = True):
    """Send an embed to the webhook. Set mention=False to skip @-tagging."""
    url = _get_webhook_url()
    if not url:
        return
    uid     = _get_user_id()
    content = f"<@{uid}>" if uid and mention else ""
    embed.setdefault("timestamp", _iso_now())
    payload = {"embeds": [embed]}
    if content:
        payload["content"] = content
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code not in (200, 204):
            log.warning(f"Discord webhook returned {resp.status_code}: {resp.text}")
    except Exception as e:
        log.warning(f"Discord alert failed: {e}")

def _send_text(line: str):
    """Send a plain-text message (no embed) — used for heartbeats."""
    url = _get_webhook_url()
    if not url:
        return
    try:
        resp = requests.post(url, json={"content": line}, timeout=5)
        if resp.status_code not in (200, 204):
            log.warning(f"Discord text returned {resp.status_code}")
    except Exception as e:
        log.warning(f"Discord text failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# Trade lifecycle alerts (most frequent — kept compact)
# ═════════════════════════════════════════════════════════════════════════════

def alert_signal(label: str, direction: str, reason: str,
                 entry: float, stop: float, target: float,
                 rr: float, paper: bool, qty: int = 1):
    """
    Signal fired — entry order about to place.

    Compact mobile layout. Direction/target/rr params ignored (long-only,
    every exit is cross-down or stop).
    """
    risk_pts = abs(entry - stop)
    desc = (
        f"**🟢 ENTRY** · `{label} ×{qty} @ {_tick_round(entry)}`\n"
        f"Stop `{_tick_round(stop)}` · Risk {risk_pts:.2f} pts"
    )
    _send_embed({
        "color": COLOR_SIGNAL,
        "description": desc,
    }, mention=False)


def alert_fill(label: str, direction: str, fill_price: float,
               stop: float, target: float, paper: bool, qty: int = 1):
    """Entry filled — bracket is now active on IBKR."""
    desc = (
        f"**✅ FILLED** · `{label} ×{qty} @ {_tick_round(fill_price)}`\n"
        f"🔴 Stop `{_tick_round(stop)}` · Exit: EMA cross-down"
    )
    _send_embed({
        "color": COLOR_FILL,
        "description": desc,
    }, mention=False)


def alert_exit(label: str, direction: str, entry: float, exit_price: float,
               pnl_dollars: float, result: str, daily_pnl: float, paper: bool,
               qty: int = 1, duration_str: str = "", nlv: float = 0.0):
    """
    Position closed. Green bar for WIN, red for LOSS.

    Direction param ignored (long-only).
    """
    is_win = result == "WIN"
    color  = COLOR_WIN if is_win else COLOR_LOSS
    icon   = "💰" if is_win else "🔻"
    label_word = "WIN" if is_win else "LOSS"

    pnl_sign = "+" if pnl_dollars >= 0 else ""
    day_sign = "+" if daily_pnl   >= 0 else ""

    duration = duration_str or "—"
    nlv_str  = f"${nlv:,.0f}" if nlv else "—"

    desc = (
        f"**{icon} {label_word} · {pnl_sign}${pnl_dollars:.2f}**\n"
        f"{label} ×{qty} · `{_tick_round(entry)} → {_tick_round(exit_price)}` · {duration}\n"
        f"Day P&L **{day_sign}${daily_pnl:.2f}** · NLV {nlv_str}"
    )
    _send_embed({
        "color": color,
        "description": desc,
    }, mention=False)


def alert_heartbeat(paper: bool, nlv: float = 0.0, uptime_str: str = "",
                    trades_today: int = 0, in_position: bool = False,
                    killed: bool = False):
    """
    Plain-text one-liner heartbeat. No embed, no @mention.

    Format: <emoji> <NLV> · <STATUS>
    Examples:
        🟢 $229,898 · FLAT
        🟡 $229,920 · IN POSITION
        🔴 $229,800 · HALTED

    uptime_str, trades_today, paper are accepted for backward compat with
    the heartbeat thread caller but not displayed.
    """
    if killed:
        emoji, status = "🔴", "HALTED"
    elif in_position:
        emoji, status = "🟡", "IN POSITION"
    else:
        emoji, status = "🟢", "FLAT"

    nlv_str = f"${nlv:,.0f}" if nlv else "—"
    _send_text(f"{emoji} {nlv_str} · {status}")


# ═════════════════════════════════════════════════════════════════════════════
# Operational alerts (less frequent, more substantial)
# ═════════════════════════════════════════════════════════════════════════════

def alert_restart(paper: bool):
    """Bot started — sent once at boot."""
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_SYSTEM,
        "description": (
            f"**🚀 BOT STARTED** · {mode}\n"
            f"{_fmt_date()} · {_fmt_time()}"
        ),
    }, mention=False)


def alert_shutdown(paper: bool, reason: str = "manual stop"):
    """Bot stopping cleanly."""
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_NEUTRAL,
        "description": f"**⏹️ BOT STOPPED** · {mode}\n{reason}",
    }, mention=False)


def alert_disconnect(paper: bool):
    """Lost IBKR connection. Reconnect loop will retry automatically."""
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_DANGER,
        "description": (
            f"**⚠️ DISCONNECTED** · {mode}\n"
            f"Broker connection lost. Attempting auto-reconnect..."
        ),
    }, mention=False)


def alert_reconnect(paper: bool, attempt: int):
    """Reconnected successfully after disconnect."""
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_SYSTEM,
        "description": f"**🔄 RECONNECTED** · {mode} · attempt {attempt}",
    }, mention=False)


# ═════════════════════════════════════════════════════════════════════════════
# Danger alerts (you NEED to know about these — they ping you)
# ═════════════════════════════════════════════════════════════════════════════

def alert_kill_switch(reason: str, daily_pnl: float, trades: int, paper: bool):
    """Bot halted for the session. Pings you."""
    sign = "+" if daily_pnl >= 0 else ""
    _send_embed({
        "color": COLOR_DANGER,
        "description": (
            f"**🛑 KILL SWITCH · Bot halted**\n"
            f"{reason}\n"
            f"Day P&L {sign}${daily_pnl:.2f} · Trades {trades}"
        ),
    })  # mention=True (default) — wakes you up


def alert_unprotected(label: str, position: int, paper: bool):
    """Position with no stop detected. Bot is emergency-flattening. Pings you."""
    _send_embed({
        "color": COLOR_DANGER,
        "description": (
            f"**🚨 UNPROTECTED POSITION · {label}**\n"
            f"{position} contracts with NO STOPS\n"
            f"Emergency flatten submitted. Bot halted — manual review needed."
        ),
    })  # mention=True — wakes you up


def alert_order_rejected(label: str, reason: str, paper: bool, code: int = 0):
    """Entry order rejected by IBKR. Stop got cancelled too. Pings you."""
    code_str = f" (code {code})" if code else ""
    _send_embed({
        "color": COLOR_DANGER,
        "description": (
            f"**❌ ORDER REJECTED · {label}**{code_str}\n"
            f"{reason[:200]}\n"
            f"Bot reset — waiting for next signal."
        ),
    })  # mention=True


# ═════════════════════════════════════════════════════════════════════════════
# Daily summary — sent once per session boundary
# ═════════════════════════════════════════════════════════════════════════════

def alert_daily_summary(daily_pnl: float, trades: int, wins: int,
                        losses: int, paper: bool, nlv: float = 0.0,
                        best_trade: float = 0.0):
    """
    End-of-session recap. Green if profitable, red if not.
    Only fires if trades > 0 (no point summarizing a zero-trade day).
    """
    is_profitable = daily_pnl >= 0
    color = COLOR_WIN if is_profitable else COLOR_LOSS
    sign  = "+" if is_profitable else ""
    win_rate = f"{round(wins / trades * 100)}%" if trades > 0 else "—"
    record   = f"{wins}W · {losses}L"
    nlv_str  = f"${nlv:,.0f}" if nlv else "—"

    desc = (
        f"**📊 {_fmt_date()} · {sign}${daily_pnl:.2f}**\n"
        f"{trades} trades · {record} · {win_rate} win rate\n"
        f"NLV {nlv_str}"
    )
    _send_embed({
        "color": color,
        "description": desc,
    }, mention=False)