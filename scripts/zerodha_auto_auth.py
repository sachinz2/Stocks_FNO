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

    # Use kite.zerodha.com so our session cookies (same domain) are sent.
    connect_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"

    def _find_token(text: str):
        m = re.search(r'request_token=([A-Za-z0-9_-]{20,})', text)
        return m.group(1) if m else None

    request_token = None
    last_url = connect_url

    # ── Approach A: follow all redirects, catch ConnectionError for localhost ──
    # If Zerodha sends a 302 to http://localhost?request_token=..., requests
    # will raise ConnectionError (localhost unreachable on server). The URL —
    # including request_token — is embedded in the exception message.
    try:
        resp = session.get(connect_url, allow_redirects=True)
        last_url = resp.url
        print(f"[auth] Approach A — Final URL: {last_url}", flush=True)
        print(f"[auth] Status: {resp.status_code}", flush=True)

        t = _find_token(last_url)
        if t:
            request_token = t
            print(f"[auth] request_token found in final URL: {t[:8]}...", flush=True)
        elif resp.status_code == 200:
            body = resp.text
            print(f"[auth] Page body (first 1000 chars):\n{body[:1000]}", flush=True)
            t = _find_token(body)
            if t:
                request_token = t
                print(f"[auth] request_token found in page body: {t[:8]}...", flush=True)

    except requests.exceptions.ConnectionError as conn_err:
        err_str = str(conn_err)
        print(f"[auth] ConnectionError (expected when Zerodha redirects to localhost):\n{err_str[:500]}", flush=True)
        t = _find_token(err_str)
        if t:
            request_token = t
            print(f"[auth] request_token extracted from ConnectionError: {t[:8]}...", flush=True)

    # ── Approach B: hop-by-hop with allow_redirects=False ──
    if not request_token:
        print("[auth] Approach A found no token. Trying hop-by-hop...", flush=True)
        current_url = connect_url
        last_resp = None
        for hop in range(10):
            last_resp = session.get(current_url, allow_redirects=False)
            loc = last_resp.headers.get("Location", "")
            print(
                f"[hop {hop}] {current_url[:80]} → {last_resp.status_code} | "
                f"Location: {loc[:100] or '(none)'}",
                flush=True,
            )

            t = _find_token(loc)
            if t:
                request_token = t
                print(f"[hop {hop}] request_token in Location header: {t[:8]}...", flush=True)
                break

            if loc and "localhost" not in loc and "127.0.0.1" not in loc:
                current_url = loc
                continue

            if last_resp.status_code == 200:
                body = last_resp.text
                print(f"[hop {hop}] 200 body (first 800):\n{body[:800]}", flush=True)
                t = _find_token(body)
                if t:
                    request_token = t
                    print(f"[hop {hop}] request_token in body: {t[:8]}...", flush=True)
                    break
            break

    if not request_token:
        raise RuntimeError(
            f"Cannot extract request_token. Last URL: {last_url}\n"
            "Open https://developers.kite.trade and confirm that your app's "
            "Redirect URL is set to exactly: http://localhost"
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
