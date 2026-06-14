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

    # ── Enctoken shortcut ────────────────────────────────────────────────────
    # After TOTP the session has an 'enctoken' cookie (Zerodha web session
    # token). Kite's backend accepts it in the standard kiteconnect
    # 'token API_KEY:ENCTOKEN' format, skipping the OAuth authorize JS page.
    enctoken = next((c.value for c in session.cookies if c.name == "enctoken"), None)
    if enctoken:
        print(f"[enctoken] Found enctoken, testing direct API access...", flush=True)
        kite.set_access_token(enctoken)
        try:
            profile = kite.profile()
            print(f"[enctoken] Direct API works! user: {profile.get('user_id')}", flush=True)
            logger.info(f"Access token (enctoken) obtained for {profile.get('user_id')}")
            return enctoken
        except Exception as enc_err:
            print(f"[enctoken] Not accepted ({enc_err}), falling back to OAuth flow...", flush=True)
            kite.access_token = None

    # ── Step 4: Walk the redirect chain and handle the authorize step ───────
    # Full flow (now that user is enabled for the app):
    #   connect/login?sess_id → connect/finish → connect/authorize → localhost?request_token=
    # The connect/authorize page is an "Allow this app?" confirmation.
    # In a browser the user clicks Allow; here we POST to it to confirm.

    def _find_token(text: str):
        m = re.search(r'request_token=([A-Za-z0-9_-]{20,})', text)
        return m.group(1) if m else None

    request_token = None
    current_url = connect_page_url  # start from the connect/login URL (has sess_id)

    for hop in range(8):
        print(f"[hop {hop}] GET {current_url[:110]}...", flush=True)
        r = session.get(current_url, allow_redirects=False)
        loc = r.headers.get("Location", "")
        print(f"[hop {hop}] → {r.status_code} | Location: {loc[:140]}", flush=True)

        # ── Found request_token in Location header ──
        t = _find_token(loc)
        if t:
            request_token = t
            print(f"[hop {hop}] request_token from Location: {t[:8]}...", flush=True)
            break

        # ── Arrived at connect/authorize ──────────────────────────────────────
        # This is a React SPA. The "Allow" button calls an internal API endpoint.
        # Try several candidates to find which one completes the authorization.
        if "connect/authorize" in current_url or "connect/authorize" in loc:
            authorize_url = loc if loc else current_url
            print(f"[hop {hop}] Reached connect/authorize: {authorize_url[:110]}", flush=True)

            enctoken = next((c.value for c in session.cookies if c.name == "enctoken"), "")
            enc_headers = {"Authorization": f"enctoken {enctoken}"} if enctoken else {}

            # Candidate A: GET /api/connect/authorize (internal REST endpoint)
            ca = session.get(
                "https://kite.zerodha.com/api/connect/authorize",
                params={"api_key": api_key, "sess_id": sess_id},
                headers=enc_headers,
                allow_redirects=False,
            )
            ca_loc = ca.headers.get("Location", "")
            print(f"[auth-A] GET /api/connect/authorize → {ca.status_code} | loc: {ca_loc[:120]} | body: {ca.text[:200]}", flush=True)
            t = _find_token(ca_loc) or _find_token(ca.text)
            if t:
                request_token = t
                print(f"[auth-A] request_token: {t[:8]}...", flush=True)
                break

            # Candidate B: POST /api/connect/authorize
            cb = session.post(
                "https://kite.zerodha.com/api/connect/authorize",
                data={"api_key": api_key, "sess_id": sess_id},
                headers=enc_headers,
                allow_redirects=False,
            )
            cb_loc = cb.headers.get("Location", "")
            print(f"[auth-B] POST /api/connect/authorize → {cb.status_code} | loc: {cb_loc[:120]} | body: {cb.text[:200]}", flush=True)
            t = _find_token(cb_loc) or _find_token(cb.text)
            if t:
                request_token = t
                print(f"[auth-B] request_token: {t[:8]}...", flush=True)
                break

            # Candidate C: GET connect/authorize with allow_redirects=True
            cc = session.get(authorize_url, allow_redirects=True)
            print(f"[auth-C] GET authorize all_redirects → {cc.status_code} | final: {cc.url[:120]}", flush=True)
            t = _find_token(cc.url) or _find_token(cc.text)
            if t:
                request_token = t
                print(f"[auth-C] request_token: {t[:8]}...", flush=True)
                break

            print("[authorize] All candidates failed — need browser Network tab to find correct endpoint.", flush=True)
            break

        # ── Stop at localhost (redirect URL) — token must be in Location header ──
        if loc and ("localhost" in loc or "127.0.0.1" in loc):
            print(f"[hop {hop}] Reached callback URL without request_token: {loc}", flush=True)
            break

        # ── Follow any other redirect ──
        if loc:
            current_url = loc
            continue

        # ── 200 response — check body for token ──
        if r.status_code == 200:
            t = _find_token(r.text)
            if t:
                request_token = t
                print(f"[hop {hop}] request_token from HTML body: {t[:8]}...", flush=True)
            else:
                print(f"[hop {hop}] 200 body (first 600):\n{r.text[:600]}", flush=True)
        else:
            print(f"[hop {hop}] body: {r.text[:400]}", flush=True)
        break

    if not request_token:
        raise RuntimeError(
            "Cannot get request_token after completing the OAuth flow. "
            "Check [hop X] lines above for where it broke."
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
