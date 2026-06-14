"""
Zerodha Automated Daily Authentication
Runs automatically at 8:30 AM IST every trading day.
Uses pyotp to generate TOTP — no manual intervention needed.
"""
import json
import logging
import os
import sys
import time

import pyotp
import redis
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.core.config import settings

logger = logging.getLogger(__name__)

REDIS_KEY = "zerodha:access_token"
LOGIN_URL = "https://kite.zerodha.com/api/login"
TWOFA_URL = "https://kite.zerodha.com/api/twofa"
SESSION_URL = "https://api.kite.trade/session/token"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Kite-Version": "3",
}


def get_redis_client():
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
    )


def generate_totp(secret: str) -> str:
    return pyotp.TOTP(secret).now()


def zerodha_auto_login() -> str:
    """
    Fully automated Zerodha login. Returns access_token.
    Requires ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET,
    ZERODHA_API_KEY, ZERODHA_API_SECRET in environment.
    """
    user_id = settings.ZERODHA_USER_ID
    password = settings.ZERODHA_PASSWORD
    totp_secret = settings.ZERODHA_TOTP_SECRET
    api_key = settings.ZERODHA_API_KEY
    api_secret = settings.ZERODHA_API_SECRET

    if not all([user_id, password, totp_secret, api_key, api_secret]):
        raise ValueError("Missing Zerodha credentials in .env")

    session = requests.Session()
    session.headers.update(HEADERS)

    # Step 1 — Login with user_id + password
    logger.info(f"Logging in to Zerodha as {user_id}...")
    resp = session.post(LOGIN_URL, data={"user_id": user_id, "password": password})
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "success":
        raise RuntimeError(f"Zerodha login failed: {data.get('message')}")

    request_id = data["data"]["request_id"]
    logger.info("Password accepted. Submitting TOTP...")

    # Step 2 — Submit TOTP (auto-generated)
    totp_code = generate_totp(totp_secret)
    resp = session.post(TWOFA_URL, data={
        "user_id": user_id,
        "request_id": request_id,
        "twofa_value": totp_code,
        "twofa_type": "totp",
        "skip_session": "",
    })
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "success":
        raise RuntimeError(f"Zerodha TOTP failed: {data.get('message')}")

    logger.info("TOTP accepted. Fetching request token...")

    # Step 3 — Get request_token via KiteConnect login URL redirect
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    resp = session.get(login_url, allow_redirects=True)
    # Extract request_token from final redirect URL
    final_url = resp.url
    if "request_token=" not in final_url:
        raise RuntimeError(f"request_token not found in redirect URL: {final_url}")

    request_token = final_url.split("request_token=")[1].split("&")[0]
    logger.info(f"Got request_token: {request_token[:8]}...")

    # Step 4 — Exchange for access_token
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]
    logger.info(f"Access token generated for {session_data.get('user_id')}")

    return access_token


def store_token(access_token: str):
    """Store access token in Redis with 24h TTL."""
    r = get_redis_client()
    r.set(REDIS_KEY, access_token, ex=86400)
    logger.info(f"Access token stored in Redis (key: {REDIS_KEY})")

    # Backup to file
    token_file = "/tmp/zerodha_token.json"
    with open(token_file, "w") as f:
        json.dump({"access_token": access_token}, f)


def run_daily_auth():
    """Entry point called by the scheduler at 8:30 AM IST."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logger.info("Starting automated Zerodha daily authentication...")

    try:
        access_token = zerodha_auto_login()
        store_token(access_token)
        logger.info("Daily authentication SUCCESSFUL")

        # Send Telegram alert
        try:
            import asyncio
            from src.notifications.telegram_service import TelegramNotifier
            notifier = TelegramNotifier()
            asyncio.run(notifier.send("Zerodha Login: SUCCESS\nReady for trading"))
        except Exception:
            pass

        return access_token

    except Exception as e:
        logger.error(f"Daily authentication FAILED: {e}")
        try:
            import asyncio
            from src.notifications.telegram_service import TelegramNotifier
            notifier = TelegramNotifier()
            asyncio.run(notifier.alert("LOGIN FAILED", str(e)))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    run_daily_auth()
