"""
Email notifier using Gmail SMTP + app password.
Replaces TelegramNotifier — same async .send() interface.
Blocking smtplib call is offloaded to a thread executor.
"""
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText

from src.core.config import settings

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


class EmailNotifier:
    """
    Sends trade alerts via Gmail SMTP.
    Disabled automatically when credentials are not configured.
    """

    def __init__(self):
        self.sender = settings.EMAIL_SENDER
        self.password = settings.EMAIL_APP_PASSWORD
        self.recipient = settings.EMAIL_RECIPIENT
        self.enabled = bool(self.sender and self.password and self.recipient)
        self.paused  = False  # toggled via POST /admin/email-alerts/pause|resume

        if not self.enabled:
            logger.warning("Email alerts disabled — set EMAIL_SENDER, EMAIL_APP_PASSWORD, EMAIL_RECIPIENT in .env")

    def _send_blocking(self, subject: str, body: str) -> bool:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[Falcon Trader] {subject}"
        msg["From"] = self.sender
        msg["To"] = self.recipient

        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(self.sender, self.password)
            smtp.sendmail(self.sender, self.recipient, msg.as_string())
        return True

    async def send(self, message: str) -> bool:
        if not self.enabled or self.paused:
            if self.paused:
                logger.debug(f"Email suppressed (paused): {message[:60]}")
            return False
        # Use first line as subject, rest as body
        lines = message.strip().splitlines()
        subject = lines[0] if lines else "Alert"
        body = "\n".join(lines)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._send_blocking, subject, body)
            logger.info(f"Email sent: {subject}")
            return True
        except Exception as exc:
            logger.error(f"Email send failed: {exc}")
            return False

    async def alert(self, title: str, body: str) -> bool:
        return await self.send(f"{title}\n{body}")

    async def order_alert(self, action: str, symbol: str, qty: int, price: float, strategy: str) -> bool:
        return await self.send(
            f"ORDER {action}\n"
            f"Strategy: {strategy}\n"
            f"Symbol: {symbol}\n"
            f"Qty: {qty} | Price: Rs{price:.2f}"
        )

    async def risk_alert(self, reason: str) -> bool:
        return await self.send(f"RISK ALERT\n{reason}")

    async def pnl_alert(self, date: str, pnl: float, positions: int) -> bool:
        direction = "PROFIT" if pnl >= 0 else "LOSS"
        return await self.send(
            f"Daily PnL Report — {direction}\n"
            f"Date: {date}\n"
            f"PnL: Rs{pnl:,.2f}\n"
            f"Open positions: {positions}"
        )
