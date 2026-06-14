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
    print("[step1] Initiating Kite Connect OAuth session...", flush=True)
    init_resp = session.get(
        f"https://kite.trade/connect/login?api_key={api_key}&v=3",
        allow_redirects=True,
    )
    connect_page_url = init_resp.url
    print(f"[step1] Final URL : {connect_page_url}", flush=True)
    print(f"[step1] Status   : {init_resp.status_code}", flush=True)
    print(f"[step1] Cookies  : {[(c.name, c.value[:12], c.domain) for c in session.cookies]}", flush=True)
    print(f"[step1] HTML (first 3000 chars):\n{init_resp.text[:3000]}", flush=True)

    page_params = parse_qs(urlparse(connect_page_url).query)
    sess_id = page_params.get("sess_id", [""])[0]
    if not sess_id:
        if "request_token=" in connect_page_url:
            tok = page_params.get("request_token", [""])[0]
            print(f"[step1] Already got request_token={tok[:8]}...", flush=True)
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
    print(f"[step3] TOTP response: {resp.text[:300]}", flush=True)
    print(f"[step3] Cookies after TOTP: {[(c.name, c.value[:12], c.domain) for c in session.cookies]}", flush=True)
    logger.info("TOTP accepted.")

    # ── Step 4: Retrieve request_token ──────────────────────────────────────
    # Try A: re-visit the connect/login URL now that we're authenticated —
    #        Zerodha may redirect us to connect/finish → localhost?request_token=
    # Try B: visit connect/finish directly with the sess_id from step 1.
    request_token = None

    for label, url in [
        ("4a-reconnect", connect_page_url),
        ("4b-finish",    f"https://kite.zerodha.com/connect/finish?api_key={api_key}&sess_id={sess_id}"),
    ]:
        if request_token:
            break
        print(f"[{label}] GET {url[:100]}...", flush=True)
        r = session.get(url, allow_redirects=False)
        loc = r.headers.get("Location", "")
        print(f"[{label}] → {r.status_code} | Location: {loc[:140]}", flush=True)
        if r.status_code not in (200, 302, 301):
            print(f"[{label}] body: {r.text[:600]}", flush=True)

        # Follow one more hop if needed (e.g. connect/login → connect/finish → localhost)
        if loc and "request_token=" not in loc and "localhost" not in loc and "127.0.0.1" not in loc:
            print(f"[{label}] Following hop to {loc[:100]}...", flush=True)
            r2 = session.get(loc, allow_redirects=False)
            loc2 = r2.headers.get("Location", "")
            print(f"[{label}] → {r2.status_code} | Location: {loc2[:140]}", flush=True)
            if r2.status_code not in (200,):
                print(f"[{label}] body: {r2.text[:600]}", flush=True)
            loc = loc2
            r = r2

        t = re.search(r'request_token=([A-Za-z0-9_-]{20,})', loc)
        if t:
            request_token = t.group(1)
            print(f"[{label}] request_token from Location: {request_token[:8]}...", flush=True)
        elif r.status_code == 200:
            t = re.search(r'request_token=([A-Za-z0-9_-]{20,})', r.text)
            if t:
                request_token = t.group(1)
                print(f"[{label}] request_token from HTML: {request_token[:8]}...", flush=True)
            else:
                print(f"[{label}] 200 body (first 800):\n{r.text[:800]}", flush=True)

    if not request_token:
        raise RuntimeError(
            "Cannot get request_token — see [step1] HTML and [step3] cookies above for clues."
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
