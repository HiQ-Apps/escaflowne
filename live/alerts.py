"""
live/alerts.py — Escaflowne
Discord webhook alerts using rich embeds.

Ported from Celeri's alerts.py. Differences:
  - BOT_NAME constant at top, easy to change for future bots
  - All "CELERI" strings replaced with the configured bot name
  - Footer mentions "MGC" instead of "MES"
  - No other logic changes — error handling, color palette, embed structure
    are identical to Celeri's battle-tested version

`target` and `rr` can be None for the cross-down-exit strategy. All alert
functions handle that gracefully.

Configuration (in .env):
  DISCORD_WEBHOOK_URL   - the channel webhook (use a different channel from
                          Celeri to keep alerts separated)
  DISCORD_USER_ID       - your Discord user ID for @mentions
"""

import logging
import os
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("escaflowne.alerts")


# ── Bot identity ──────────────────────────────────────────────────────────────
# Change BOT_NAME if cloning for a third bot. Everything downstream uses it.
BOT_NAME    = "ESCAFLOWNE"
BOT_DIAMOND = "🜚"          # alchemical symbol for gold (was ◆ for Celeri)
INSTRUMENT  = "MGC LIVE"   # appears in restart/footer messages


# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_WIN     = 0xFFD700  # bright gold (was 0x00DD88 lime for Celeri)
COLOR_LOSS    = 0xFF3A4A  # bright red
COLOR_SIGNAL  = 0xC9A35A  # tarnished gold — new signal
COLOR_FILL    = 0xFFB020  # amber — filled
COLOR_SYSTEM  = 0x7C83FD  # periwinkle — system
COLOR_NEUTRAL = 0x4A5568  # dark gray
COLOR_DANGER  = 0xFF3A4A  # red — warnings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_webhook_url():
    return os.getenv("DISCORD_WEBHOOK_URL", "")

def _get_user_id():
    return os.getenv("DISCORD_USER_ID", "")

def _tick_round(price: float, tick: float = 0.10) -> str:
    """MGC tick = $0.10 (vs Celeri's MES which was 0.25)."""
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
    url = _get_webhook_url()
    if not url:
        return
    uid      = _get_user_id()
    content  = f"<@{uid}>" if uid and mention else ""
    embed.setdefault("author", {"name": f"{BOT_DIAMOND} {BOT_NAME}"})
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

def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": value, "inline": inline}

def _divider() -> dict:
    return {"name": "\u200b", "value": "\u200b", "inline": False}


# ── Public alert functions ────────────────────────────────────────────────────

def alert_signal(label: str, direction: str, reason: str,
                 entry: float, stop: float, target: float,
                 rr: float, paper: bool, qty: int = 1):
    """Signal fired — entry about to place."""
    mode     = "PAPER" if paper else "LIVE"
    risk_pts = abs(entry - stop)
    arrow    = "📈" if direction == "LONG" else "📉"
    dir_str  = "LONG  ▲" if direction == "LONG" else "SHORT  ▼"

    if target is not None:
        rwd_pts    = abs(target - entry)
        target_str = f"`{_tick_round(target)}`"
        reward_str = f"{rwd_pts:.2f} pts"
        rr_str     = f"1 : {rr:.2f}" if rr is not None else f"1 : {rwd_pts/risk_pts:.2f}"
    else:
        target_str = "`cross-down exit`"
        reward_str = "EMA cross"
        rr_str     = "dynamic"

    _send_embed({
        "color": COLOR_SIGNAL,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}  ·  {_fmt_time()}"},
        "title": f"{arrow}  {label} {dir_str}",
        "fields": [
            _field("Entry",     f"`{_tick_round(entry)}`"),
            _field("Stop",      f"`{_tick_round(stop)}`"),
            _field("Target",    target_str),
            _divider(),
            _field("Risk",      f"{risk_pts:.2f} pts"),
            _field("Reward",    reward_str),
            _field("R:R",       rr_str),
            _divider(),
            _field("Contracts", str(qty)),
            _field("Signal",    reason),
            _field("Status",    "⏳  Waiting for fill..."),
        ],
        "footer": {"text": f"{mode}  ·  ORDER PLACED"},
    })


def alert_fill(label: str, direction: str, fill_price: float,
               stop: float, target: float, paper: bool, qty: int = 1):
    """Entry filled."""
    mode  = "PAPER" if paper else "LIVE"
    arrow = "📈" if direction == "LONG" else "📉"

    target_field = (
        _field("Target", f"`{_tick_round(target)}`   🟢")
        if target is not None
        else _field("Target", "`cross-down exit`   🔄")
    )

    _send_embed({
        "color": COLOR_FILL,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}  ·  {_fmt_time()}"},
        "title": f"✅  FILLED  —  {label} {direction}",
        "description": f"**Entry @ `{_tick_round(fill_price)}`**\nBracket is now active.",
        "fields": [
            _field("Stop",      f"`{_tick_round(stop)}`   🔴"),
            target_field,
            _field("Contracts", str(qty)),
        ],
        "footer": {"text": f"{mode}  ·  BRACKET ACTIVE"},
    })


def alert_exit(label: str, direction: str, entry: float, exit_price: float,
               pnl_dollars: float, result: str, daily_pnl: float, paper: bool,
               qty: int = 1, duration_str: str = "", nlv: float = 0.0):
    mode   = "PAPER" if paper else "LIVE"
    is_win = result == "WIN"
    color  = COLOR_WIN if is_win else COLOR_LOSS
    emoji  = "🟢" if is_win else "🔴"
    sign   = "+" if pnl_dollars >= 0 else ""
    d_sign = "+" if daily_pnl  >= 0 else ""

    _send_embed({
        "color": color,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}  ·  {_fmt_time()}"},
        "title": f"{emoji}  {result}  —  {label} {direction}",
        "description": f"### {sign}${pnl_dollars:.2f}",
        "fields": [
            _field("Entry",     f"`{_tick_round(entry)}`"),
            _field("Exit",      f"`{_tick_round(exit_price)}`"),
            _field("Duration",  duration_str or "—"),
            _divider(),
            _field("Contracts", str(qty)),
            _field("Day P&L",   f"{d_sign}${daily_pnl:.2f}"),
            _field("NLV",       f"${nlv:,.2f}" if nlv else "—"),
        ],
        "footer": {"text": f"{mode}  ·  POSITION CLOSED"},
    })


def alert_kill_switch(reason: str, daily_pnl: float, trades: int, paper: bool):
    mode  = "PAPER" if paper else "LIVE"
    sign  = "+" if daily_pnl >= 0 else ""
    _send_embed({
        "color": COLOR_DANGER,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}  ·  {_fmt_time()}"},
        "title": "🛑  KILL SWITCH  —  Bot halted",
        "fields": [
            _field("Reason",   reason,                        inline=False),
            _field("Day P&L",  f"{sign}${daily_pnl:.2f}"),
            _field("Trades",   str(trades)),
        ],
        "footer": {"text": f"{mode}  ·  HALTED FOR SESSION"},
    })


def alert_daily_summary(daily_pnl: float, trades: int, wins: int,
                        losses: int, paper: bool, nlv: float = 0.0,
                        best_trade: float = 0.0):
    mode     = "PAPER" if paper else "LIVE"
    sign     = "+" if daily_pnl   >= 0 else ""
    b_sign   = "+" if best_trade  >= 0 else ""
    win_rate = f"{round(wins / trades * 100)}%" if trades > 0 else "—"
    record   = f"{wins}W / {losses}L"
    emoji    = "🟢" if daily_pnl >= 0 else "🔴"

    _send_embed({
        "color": COLOR_WIN if daily_pnl >= 0 else COLOR_LOSS,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}"},
        "title": f"{emoji}  DAILY SUMMARY  —  {_fmt_date()}",
        "description": f"### {sign}${abs(daily_pnl):.2f}",
        "fields": [
            _field("Trades",     str(trades)),
            _field("Record",     record),
            _field("Win Rate",   win_rate),
            _divider(),
            _field("NLV",        f"${nlv:,.2f}" if nlv else "—"),
            _field("Best Trade", f"{b_sign}${abs(best_trade):.2f}" if trades > 0 else "—"),
        ],
        "footer": {"text": f"{mode}  ·  Session ended"},
    })


def alert_disconnect(paper: bool):
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_DANGER,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}  ·  {_fmt_time()}"},
        "title": "⚠️  DISCONNECTED",
        "description": "Broker connection lost.\nAttempting auto-reconnect...",
        "footer": {"text": mode},
    })


def alert_reconnect(paper: bool, attempt: int):
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_SYSTEM,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}  ·  {_fmt_time()}"},
        "title": f"🔄  RECONNECTED  —  attempt {attempt}",
        "footer": {"text": mode},
    }, mention=False)


def alert_restart(paper: bool):
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_SYSTEM,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}"},
        "title": "🚀  BOT STARTED",
        "description": f"{_fmt_date()}  ·  {_fmt_time()}",
        "footer": {"text": f"{mode}  ·  {INSTRUMENT}"},
    }, mention=False)


def alert_shutdown(paper: bool, reason: str = "manual stop"):
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_NEUTRAL,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}"},
        "title": "⏹️  BOT STOPPED",
        "description": reason,
        "footer": {"text": mode},
    }, mention=False)


def alert_unprotected(label: str, position: int, paper: bool):
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_DANGER,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}  ·  {_fmt_time()}"},
        "title": f"🚨  UNPROTECTED POSITION  —  {label}",
        "description": f"**{position} contracts with NO STOPS**\nEmergency flatten submitted.\nBot halted — manual review required.",
        "footer": {"text": f"{mode}  ·  EMERGENCY FLATTEN"},
    })


def alert_heartbeat(paper: bool, nlv: float = 0.0, uptime_str: str = "",
                    trades_today: int = 0):
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_NEUTRAL,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}"},
        "title": "💓  Heartbeat",
        "fields": [
            _field("Status",  "Online  ✅",                    inline=False),
            _field("Uptime",  uptime_str or "—",               inline=False),
            _field("NLV",     f"${nlv:,.2f}" if nlv else "—",  inline=False),
            _field("Trades",  f"{trades_today} today",         inline=False),
        ],
        "footer": {"text": f"{mode}  ·  {_fmt_time()}"},
    }, mention=False)


def alert_order_rejected(label: str, reason: str, paper: bool, code: int = 0):
    mode = "PAPER" if paper else "LIVE"
    _send_embed({
        "color": COLOR_DANGER,
        "author": {"name": f"{BOT_DIAMOND} {BOT_NAME}  ·  {mode}  ·  {_fmt_time()}"},
        "title": f"❌  ORDER REJECTED  —  {label}",
        "description": reason[:200],
        "fields": [
            _field("Code", str(code) if code else "—"),
            _field("Action", "Bot reset — waiting for next signal"),
        ],
        "footer": {"text": f"{mode}  ·  Check margin/funds if recurring"},
    })