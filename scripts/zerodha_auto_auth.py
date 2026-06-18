"""
Zerodha Automated Daily Authentication
Runs automatically at 8:30 AM IST every trading day.
Uses pyotp to generate TOTP — no manual intervention needed.

Flow:
  1. GET connect/login (unauthenticated) → get sess_id cookie
  2. POST /api/login with credentials → get request_id
  3. POST /api/twofa with TOTP → session authenticated
  4. GET connect/login → 302 → connect/finish → 302 → localhost?request_token=...
  5. Exchange request_token for access_token via kite.generate_session()
  6. Store access_token in Redis with 24h TTL
"""
import json
import logging
import os
import re
import sys

import pyotp
import redis
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.core.config import settings

logger = logging.getLogger(__name__)

REDIS_KEY = "zerodha:access_token"
LOGIN_URL = "https://kite.zerodha.com/api/login"
TWOFA_URL = "https://kite.zerodha.com/api/twofa"

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


def zerodha_auto_login() -> str:
    """Automated Zerodha OAuth login. Returns access_token."""
    from urllib.parse import parse_qs, urlparse
    from kiteconnect import KiteConnect

    api_key = settings.ZERODHA_API_KEY
    api_secret = settings.ZERODHA_API_SECRET
    user_id = settings.ZERODHA_USER_ID
    password = settings.ZERODHA_PASSWORD
    totp_secret = settings.ZERODHA_TOTP_SECRET

    if not all([user_id, password, api_key, api_secret, totp_secret]):
        raise ValueError("Missing Zerodha credentials in .env")

    kite = KiteConnect(api_key=api_key)
    session = requests.Session()
    session.headers.update(HEADERS)

    # Step 1: Establish Connect OAuth session (must be before login)
    init_resp = session.get(
        f"https://kite.trade/connect/login?api_key={api_key}&v=3",
        allow_redirects=True,
    )
    connect_page_url = init_resp.url
    page_params = parse_qs(urlparse(connect_page_url).query)
    sess_id = page_params.get("sess_id", [""])[0]

    # Edge case: already authenticated from a previous run
    if not sess_id:
        if "request_token=" in connect_page_url:
            tok = page_params.get("request_token", [""])[0]
            sd = kite.generate_session(tok, api_secret=api_secret)
            return sd["access_token"]
        raise RuntimeError(f"No sess_id in connect page URL: {connect_page_url}")

    logger.info("Connect session established.")

    # Step 2: Login
    resp = session.post(LOGIN_URL, data={"user_id": user_id, "password": password})
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Login failed: {data.get('message')}")
    request_id = data["data"]["request_id"]
    logger.info("Password accepted.")

    # Step 3: TOTP
    resp = session.post(TWOFA_URL, data={
        "user_id": user_id,
        "request_id": request_id,
        "twofa_value": pyotp.TOTP(totp_secret).now(),
        "twofa_type": "totp",
        "skip_session": "",
    })
    resp.raise_for_status()
    if resp.json().get("status") != "success":
        raise RuntimeError(f"TOTP failed: {resp.json().get('message')}")
    logger.info("TOTP accepted.")

    # Step 4: Follow redirect chain to get request_token
    # connect/login → 302 → connect/finish → 302 → localhost?request_token=...
    _token_re = re.compile(r'request_token=([A-Za-z0-9_-]{20,})')
    request_token = None
    current_url = connect_page_url

    for _ in range(8):
        r = session.get(current_url, allow_redirects=False)
        loc = r.headers.get("Location", "")

        m = _token_re.search(loc) or _token_re.search(r.text)
        if m:
            request_token = m.group(1)
            break

        if loc:
            current_url = loc
        else:
            break

    if not request_token:
        raise RuntimeError("request_token not found in OAuth redirect chain.")

    logger.info(f"request_token obtained: {request_token[:8]}...")

    # Step 5: Exchange for access_token
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]
    logger.info(f"Access token generated for {session_data.get('user_id')}")
    return access_token


def store_token(access_token: str):
    """Store access token in Redis with 24h TTL."""
    r = get_redis_client()
    r.set(REDIS_KEY, access_token, ex=86400)
    logger.info(f"Access token stored in Redis (key: {REDIS_KEY})")
    with open("/tmp/zerodha_token.json", "w") as f:
        json.dump({"access_token": access_token}, f)


def fetch_and_cache_lot_sizes(access_token: str) -> int:
    """
    Fetch all NFO instruments from Kite and cache lot sizes in Redis.
    Called once daily after auth so the engine always has fresh lot sizes.
    Returns number of symbols cached.
    """
    from kiteconnect import KiteConnect
    from src.core.constants import FNO_SYMBOLS, REDIS_LOT_SIZE_PREFIX

    kite = KiteConnect(api_key=settings.ZERODHA_API_KEY)
    kite.set_access_token(access_token)

    instruments = kite.instruments("NFO")
    r = get_redis_client()
    fno_set = set(FNO_SYMBOLS)
    seen: set = set()

    for inst in instruments:
        name = inst.get("name", "")
        if name in fno_set and name not in seen:
            lot_size = inst.get("lot_size", 0)
            if lot_size > 0:
                r.set(f"{REDIS_LOT_SIZE_PREFIX}{name}", str(lot_size), ex=86400 * 7)
                seen.add(name)

    logger.info(f"Lot sizes cached for {len(seen)} F&O symbols")
    return len(seen)


def run_daily_auth():
    """Entry point called by the scheduler at 8:30 AM IST."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logger.info("Starting Zerodha daily authentication...")
    try:
        access_token = zerodha_auto_login()
        store_token(access_token)
        logger.info("Daily authentication SUCCESSFUL")

        # Refresh lot sizes from live instrument data
        try:
            count = fetch_and_cache_lot_sizes(access_token)
            logger.info(f"Instrument lot sizes refreshed: {count} symbols")
        except Exception as e:
            logger.warning(f"Lot size refresh failed (non-fatal, using hardcoded fallback): {e}")

        return access_token
    except Exception as e:
        logger.error(f"Daily authentication FAILED: {e}")
        try:
            import asyncio
            from src.notifications.email_service import EmailNotifier
            asyncio.run(EmailNotifier().alert("LOGIN FAILED", str(e)))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    run_daily_auth()
