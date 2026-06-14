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

    if not all([user_id, password, api_key, api_secret]):
        raise ValueError("Missing Zerodha credentials in .env")

    # Determine 2FA method
    if totp_secret:
        twofa_value = generate_totp(totp_secret)
        twofa_type = "totp"
        logger.info("Using TOTP for 2FA")
    else:
        twofa_value = os.environ.get("ZERODHA_PIN", "")
        twofa_type = "user_pin"
        if not twofa_value:
            raise ValueError("Set either ZERODHA_TOTP_SECRET or ZERODHA_PIN in .env")
        logger.info("Using static PIN for 2FA")

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
    logger.info("Password accepted. Submitting 2FA...")

    # Step 2 — Submit 2FA (TOTP or static PIN)
    resp = session.post(TWOFA_URL, data={
        "user_id": user_id,
        "request_id": request_id,
        "twofa_value": twofa_value,
        "twofa_type": twofa_type,
        "skip_session": "",
    })
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "success":
        raise RuntimeError(f"Zerodha TOTP failed: {data.get('message')}")

    logger.info("TOTP accepted. Fetching request token...")

    # Step 3 — Walk the KiteConnect redirect chain manually.
    # Zerodha's connect/finish page is a 200 HTML with a JS redirect
    # (window.location = "http://localhost?request_token=...") that requests
    # cannot follow. We hop one redirect at a time with allow_redirects=False,
    # stop before attempting localhost, and parse the token from either the
    # Location header or the HTML body.
    import re
    from urllib.parse import parse_qs, urlparse
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    current_url = kite.login_url()
    request_token = None
    last_resp = None

    for _hop in range(10):
        last_resp = session.get(current_url, allow_redirects=False)
        location = last_resp.headers.get("Location", "")

        # Happy path: request_token is in the Location header (HTTP redirect)
        if "request_token=" in location:
            request_token = parse_qs(urlparse(location).query).get("request_token", [""])[0]
            break

        # Don't try to connect to localhost — it can't be reached from the server.
        # The request_token should have been in the Location header; if not, fall through.
        if location and ("localhost" in location or "127.0.0.1" in location):
            logger.warning(f"Redirect target is localhost but no request_token: {location}")
            break

        # Follow any other redirect
        if location:
            logger.debug(f"Redirect hop {_hop}: {location[:80]}")
            current_url = location
            continue

        # 200 response — parse the JS redirect in the finish page HTML
        if last_resp.status_code == 200:
            body = last_resp.text
            match = re.search(r'request_token=([A-Za-z0-9_-]+)', body)
            if match:
                request_token = match.group(1)
                break

            # Fallback: sess_id in the finish URL sometimes equals request_token
            params = parse_qs(urlparse(last_resp.url).query)
            sess_id = params.get("sess_id", [""])[0]
            if sess_id:
                logger.warning("Falling back to sess_id as request_token — verify this works.")
                request_token = sess_id
        break

    if not request_token:
        raise RuntimeError(
            f"Could not extract request_token after following the redirect chain. "
            f"Last URL: {last_resp.url if last_resp else current_url}\n"
            "Check that ZERODHA_API_KEY redirect URL is set to http://localhost in "
            "your Kite Connect app settings."
        )
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
