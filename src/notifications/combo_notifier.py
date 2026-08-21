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

    # Fixed 2026-08-21 (deep review): admin_router's /email-alerts/pause|
    # resume and /email-alerts/status read/write notifier.paused and
    # notifier.enabled directly (EmailNotifier's own attributes) -- wiring
    # ComboNotifier in as the app's notifier (replacing a bare
    # EmailNotifier(), so TelegramNotifier -- fully implemented, properly
    # timeout-bounded, but never actually instantiated in production before
    # this fix -- provides real redundancy against an email-side hang or
    # outage) needs the same surface so those endpoints keep working.
    # Pausing/enabled reflects EMAIL specifically, matching what those
    # "email-alerts" endpoints are named and documented to control; the
    # underlying pause doesn't affect Telegram delivery.
    @property
    def paused(self) -> bool:
        return self.email.paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self.email.paused = value

    @property
    def enabled(self) -> bool:
        return self.email.enabled or self.telegram.enabled

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
