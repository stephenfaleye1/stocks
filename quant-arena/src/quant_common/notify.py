"""Discord / Telegram alerts. Failures are logged, never raised into trading paths."""
from __future__ import annotations

import logging

import httpx

from .settings import Settings

log = logging.getLogger(__name__)


def send_alert(settings: Settings, text: str, *, timeout: float = 5.0) -> dict[str, bool]:
    """Send `text` to every configured channel. Returns {channel: delivered}."""
    results: dict[str, bool] = {}
    prefix = f"[quant-arena:{settings.trading_mode.upper()}] "

    discord = settings.discord_webhook_url.get_secret_value()
    if discord:
        try:
            r = httpx.post(discord, json={"content": prefix + text[:1900]}, timeout=timeout)
            results["discord"] = r.status_code in (200, 204)
        except httpx.HTTPError as exc:
            log.warning("discord alert failed: %s", exc)
            results["discord"] = False

    token = settings.telegram_bot_token.get_secret_value()
    if token and settings.telegram_chat_id:
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": prefix + text[:4000]},
                timeout=timeout,
            )
            results["telegram"] = r.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("telegram alert failed: %s", exc)
            results["telegram"] = False

    return results
