"""
Combined notifier: sends every alert to both Email and Telegram simultaneously.
Either channel failing does not block the other.
"""
import asyncio
import logging
from typing import List

from src.notifications.email_service import EmailNotifier
from src.notifications.telegram_service import TelegramNotifier

logger = logging.getLogger(__name__)


class ComboNotifier:
    """
    Drop-in replacement for EmailNotifier or TelegramNotifier.
    Exposes the same .send(message) interface used by LiveTradingEngine.
    """

    def __init__(self):
        self.email = EmailNotifier()
        self.telegram = TelegramNotifier()
        channels = []
        if self.email.enabled:
            channels.append("email")
        if self.telegram.enabled:
            channels.append("telegram")
        if channels:
            logger.info(f"ComboNotifier: active channels = {channels}")
        else:
            logger.warning("ComboNotifier: no notification channels configured.")

    async def send(self, message: str) -> bool:
        results = await asyncio.gather(
            self.email.send(message),
            self.telegram.send(message),
            return_exceptions=True,
        )
        ok = any(r is True for r in results)
        if not ok:
            logger.warning(f"All notification channels failed for: {message[:80]}")
        return ok
