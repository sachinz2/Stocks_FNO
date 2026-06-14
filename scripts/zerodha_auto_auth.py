"""
Zerodha Automated Daily Authentication
Runs automatically at 8:30 AM IST every trading day.
Uses pyotp to generate TOTP — no manual intervention needed.
"""
import json
import logging
import os
import re
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

    Order matters: Connect OAuth session must be established BEFORE login.
    If login happens first and we visit connect/login afterwards, Zerodha
    skips the auth page but connect/finish returns 400 because the connect
    session was never linked to the login. The correct order:
      1. Visit connect/login (unauthenticated) → get sess_id
      2. Login via /api/login in the same session (connect context cookie present)
      3. TOTP via /api/twofa in the same session
      4. Visit connect/finish → get 302 to localhost?request_token=...
      5. Exchange request_token for access_token
    """
    import re
    from urllib.parse import parse_qs, urlparse
    from kiteconnect import KiteConnect

    api_key = settings.ZERODHA_API_KEY
    api_secret = settings.ZERODHA_API_SECRET
    user_id = settings.ZERODHA_USER_ID
    password = settings.ZERODHA_PASSWORD
    totp_secret = settings.ZERODHA_TOTP_SECRET

    if not all([user_id, password, api_key, api_secret]):
        raise ValueError("Missing Zerodha credentials in .env")
    if not totp_secret:
        raise ValueError("ZERODHA_TOTP_SECRET is required for automated login")

    kite = KiteConnect(api_key=api_key)

    # Fresh session — no prior cookies, so Zerodha shows the connect login page
    session = requests.Session()
    session.headers.update(HEADERS)

    # ── Step 1: Establish the Connect OAuth session ──────────────────────────
    # Visit connect/login unauthenticated. Zerodha sets a connect-context cookie
    # and returns a sess_id in the URL. Login must happen inside this context.
    print("[step1] Initiating Kite Connect OAuth session...", flush=True)
    init_resp = session.get(
        f"https://kite.trade/connect/login?api_key={api_key}&v=3",
        allow_redirects=True,
    )
    connect_page_url = init_resp.url
    print(f"[step1] Connect page: {connect_page_url}", flush=True)

    page_params = parse_qs(urlparse(connect_page_url).query)
    sess_id = page_params.get("sess_id", [""])[0]
    if not sess_id:
        # Already authenticated (shouldn't happen with fresh session, but handle it)
        if "request_token=" in connect_page_url:
            tok = page_params.get("request_token", [""])[0]
            print(f"[step1] Surprise: already got request_token={tok[:8]}...", flush=True)
            sd = kite.generate_session(tok, api_secret=api_secret)
            return sd["access_token"]
        raise RuntimeError(f"No sess_id in connect page URL: {connect_page_url}")
    print(f"[step1] sess_id: {sess_id[:8]}...", flush=True)

    # ── Step 2: Login inside the connect context ─────────────────────────────
    print(f"[step2] Logging in as {user_id}...", flush=True)
    resp = session.post(LOGIN_URL, data={"user_id": user_id, "password": password})
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Zerodha login failed: {data.get('message')}")
    request_id = data["data"]["request_id"]
    logger.info("Password accepted. Submitting 2FA...")

    # ── Step 3: TOTP inside the connect context ─────────────────────────────
    totp_value = generate_totp(totp_secret)
    print(f"[step3] Submitting TOTP...", flush=True)
    resp = session.post(TWOFA_URL, data={
        "user_id": user_id,
        "request_id": request_id,
        "twofa_value": totp_value,
        "twofa_type": "totp",
        "skip_session": "",
    })
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Zerodha TOTP failed: {data.get('message')}")
    logger.info("TOTP accepted.")

    # ── Step 4: Complete connect flow to get request_token ───────────────────
    # The session now has BOTH the connect context (sess_id cookie from step 1)
    # AND the authenticated user (from steps 2-3). connect/finish should now
    # return a 302 to http://localhost?request_token=... instead of 400.
    finish_url = f"https://kite.zerodha.com/connect/finish?api_key={api_key}&sess_id={sess_id}"
    print(f"[step4] Visiting connect/finish with sess_id={sess_id[:8]}...", flush=True)

    finish_resp = session.get(finish_url, allow_redirects=False)
    location = finish_resp.headers.get("Location", "")
    print(f"[step4] → {finish_resp.status_code} | Location: {location[:140]}", flush=True)

    request_token = None

    if "request_token=" in location:
        from urllib.parse import parse_qs, urlparse
        request_token = parse_qs(urlparse(location).query).get("request_token", [""])[0]
        print(f"[step4] request_token from Location header: {request_token[:8]}...", flush=True)
    elif finish_resp.status_code == 200:
        body = finish_resp.text
        print(f"[step4] 200 body (first 800 chars):\n{body[:800]}", flush=True)
        match = re.search(r'request_token=([A-Za-z0-9_-]{20,})', body)
        if match:
            request_token = match.group(1)
            print(f"[step4] request_token from HTML body: {request_token[:8]}...", flush=True)
    else:
        print(f"[step4] {finish_resp.status_code} body:\n{finish_resp.text[:400]}", flush=True)

    if not request_token:
        raise RuntimeError(
            f"connect/finish returned {finish_resp.status_code} — cannot get request_token.\n"
            f"Location header: {location or '(none)'}\n"
            "Verify redirect URL in Kite Connect app is set to http://localhost"
        )

    logger.info(f"Got request_token: {request_token[:8]}...")

    # ── Step 5: Exchange request_token for access_token ──────────────────────
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
