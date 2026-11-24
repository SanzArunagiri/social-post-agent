"""Telegram notifications (optional): TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID."""
from __future__ import annotations

import logging

from .config import settings
from .net import client, ok

log = logging.getLogger("agent.notify")


async def send_telegram(text: str, chat_id: str = "", silent: bool = False) -> dict:
    if not settings.telegram_bot_token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN (and TELEGRAM_CHAT_ID) in .env to send Telegram messages")
    chat = (chat_id or settings.telegram_chat_id).strip()
    if not chat:
        raise ValueError("Set TELEGRAM_CHAT_ID in .env or pass chat_id")
    text = (text or "").strip()
    if not text:
        raise ValueError("Nothing to send")
    async with client(30) as c:
        d = ok(await c.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage", json={
            "chat_id": chat, "text": text[:4000], "disable_web_page_preview": True, "disable_notification": silent}))
    return {"sent": True, "message_id": (d.get("result") or {}).get("message_id")}


async def notify(text: str, failure: bool = False) -> None:
    """Best-effort notification about a publish result (TELEGRAM_NOTIFY = all | failures | off)."""
    mode = settings.telegram_notify
    if mode == "off" or (mode == "failures" and not failure) or not settings.configured()["telegram"]:
        return
    try:
        await send_telegram(text)
    except Exception as exc:
        log.warning("telegram notification failed: %s", exc)
