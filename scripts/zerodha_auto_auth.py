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

    import re
    from urllib.parse import parse_qs, urlparse
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)

    # Use kite.zerodha.com (same domain as our login session cookies) NOT
    # kite.trade — cross-domain cookies are not sent, causing auth to fail.
    connect_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"

    request_token = None
    current_url = connect_url
    last_resp = None

    for _hop in range(10):
        last_resp = session.get(current_url, allow_redirects=False)
        location = last_resp.headers.get("Location", "")

        logger.info(
            f"[hop {_hop}] GET {current_url[:90]} "
            f"→ {last_resp.status_code} | Location: {location[:100] or '(none)'}"
        )

        # Best case: request_token is already in the Location header
        if "request_token=" in location:
            request_token = parse_qs(urlparse(location).query).get("request_token", [""])[0]
            logger.info(f"[hop {_hop}] request_token found in Location header")
            break

        # Redirect to our app's callback URL (e.g. http://localhost).
        # Even if the URL itself has no request_token, try to parse it;
        # otherwise, fall through to HTML / sess_id extraction below.
        if location and ("localhost" in location or "127.0.0.1" in location):
            logger.info(f"[hop {_hop}] Redirect to app callback: {location}")
            # No request_token in the location — fall through to HTML parse

        # Follow any other off-site redirect
        elif location:
            current_url = location
            continue

        # ----- 200 response: parse the HTML body -----
        if last_resp.status_code == 200:
            body = last_resp.text
            logger.info(f"[hop {_hop}] HTML body (first 800 chars):\n{body[:800]}")

            # Most common: window.location = "http://localhost?request_token=XYZ"
            match = re.search(r'request_token[="\s:]+([A-Za-z0-9_-]{20,})', body)
            if match:
                request_token = match.group(1)
                logger.info(f"[hop {_hop}] request_token found in HTML body")
                break

        # ----- Last resort: sess_id from the finish URL -----
        params = parse_qs(urlparse(last_resp.url).query)
        sess_id = params.get("sess_id", [""])[0]
        if sess_id:
            logger.warning(
                f"[hop {_hop}] Could not find request_token in redirect or HTML. "
                f"Trying sess_id as request_token: {sess_id[:8]}..."
            )
            request_token = sess_id

        break  # Nothing more to try

    if not request_token:
        raise RuntimeError(
            f"Could not extract request_token after following the redirect chain.\n"
            f"Last URL: {last_resp.url if last_resp else current_url}\n"
            "Verify your Kite Connect app's redirect URL is http://localhost "
            "in the Kite Connect Developer Console."
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
