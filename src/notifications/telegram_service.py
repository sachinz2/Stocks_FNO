import logging
import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """
    Sends alerts to a Telegram chat via Bot API.
    Disabled automatically when token/chat_id are not configured.
    """

    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token or settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)

        if not self.enabled:
            logger.warning("Telegram alerts disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    async def send(self, message: str) -> bool:
        if not self.enabled:
            return False
        try:
            url = _SEND_URL.format(token=self.token)
            payload = {
                "chat_id": self.chat_id,
                "text": f"🦅 <b>Falcon Trader</b>\n\n{message}",
                "parse_mode": "HTML",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.error(f"Telegram error {resp.status_code}: {resp.text}")
                return resp.status_code == 200
        except Exception as exc:
            logger.error(f"Telegram send failed: {exc}")
            return False

    async def alert(self, title: str, body: str) -> bool:
        return await self.send(f"<b>{title}</b>\n{body}")

    async def order_alert(self, action: str, symbol: str, qty: int, price: float, strategy: str) -> bool:
        return await self.send(
            f"<b>ORDER {action}</b>\n"
            f"Strategy: {strategy}\n"
            f"Symbol: {symbol}\n"
            f"Qty: {qty} | Price: ₹{price:.2f}"
        )

    async def risk_alert(self, reason: str) -> bool:
        return await self.send(f"⚠️ <b>RISK ALERT</b>\n{reason}")

    async def pnl_alert(self, date: str, pnl: float, positions: int) -> bool:
        emoji = "🟢" if pnl >= 0 else "🔴"
        return await self.send(
            f"{emoji} <b>Daily PnL Report</b>\n"
            f"Date: {date}\n"
            f"PnL: ₹{pnl:,.2f}\n"
            f"Open positions: {positions}"
        )
