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


def _playwright_authorize(authorize_url: str, cookies) -> str:
    """
    Use headless Chromium to click the Allow button on Zerodha's Connect
    authorization page (a React SPA that can't be driven by requests alone).

    Transfers the authenticated session cookies from the requests.Session so
    Playwright starts already logged in.  Intercepts every navigation to catch
    the redirect to the callback URL which carries request_token.
    """
    import re as _re
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    _tok = _re.compile(r'request_token=([A-Za-z0-9_-]{20,})')
    found_token = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context()

        # Transfer session cookies so the browser is already authenticated
        for c in cookies:
            try:
                ctx.add_cookies([{
                    "name":   c.name,
                    "value":  c.value,
                    "domain": c.domain or "kite.zerodha.com",
                    "path":   c.path or "/",
                }])
            except Exception:
                pass

        page = ctx.new_page()

        # Intercept every request — callback URL arrives as a navigation event
        def _on_request(req):
            if "request_token=" in req.url:
                m = _tok.search(req.url)
                if m:
                    found_token.append(m.group(1))

        page.on("request", _on_request)

        logger.info(f"Playwright: navigating to {authorize_url[:80]}")
        page.goto(authorize_url, wait_until="networkidle", timeout=20_000)

        if found_token:
            browser.close()
            return found_token[0]

        # Find and click the Authorize / Allow button
        clicked = False
        for selector in [
            "button[type='submit']",
            "button:has-text('Allow')",
            "button:has-text('Authorize')",
            "button:has-text('Confirm')",
            ".button-blue",
            "input[type='submit']",
        ]:
            try:
                page.click(selector, timeout=4_000)
                logger.info(f"Playwright: clicked '{selector}'")
                clicked = True
                break
            except PWTimeout:
                continue

        if not clicked:
            logger.warning("Playwright: no known authorize button found — dumping page text")
            logger.warning(page.inner_text("body")[:800])
            browser.close()
            raise RuntimeError("Playwright: could not find Authorize button on connect/authorize page.")

        # Wait for the callback redirect (up to 10 s)
        try:
            page.wait_for_function(
                "() => window.location.href.includes('request_token=')",
                timeout=10_000,
            )
            m = _tok.search(page.url)
            if m:
                found_token.append(m.group(1))
        except PWTimeout:
            pass

        if not found_token:
            # Last resort — check any intercepted request
            logger.warning(f"Playwright final URL: {page.url}")

        browser.close()

    if not found_token:
        raise RuntimeError("Playwright: request_token not found after clicking Authorize.")

    logger.info(f"Playwright: request_token obtained: {found_token[0][:8]}...")
    return found_token[0]


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

    # Step 1: Visit connect login to establish session cookie.
    # Zerodha now tracks the OAuth session via cookie, not sess_id in the URL.
    # We just need to hit the page so the server sets the session cookie on our
    # requests.Session object before we POST credentials.
    connect_page_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    init_resp = session.get(
        f"https://kite.trade/connect/login?api_key={api_key}&v=3",
        allow_redirects=True,
    )

    # Edge case: already authenticated from a previous run
    if "request_token=" in init_resp.url:
        params = parse_qs(urlparse(init_resp.url).query)
        tok = params.get("request_token", [""])[0]
        if tok:
            sd = kite.generate_session(tok, api_secret=api_secret)
            return sd["access_token"]

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

    # Step 4: Re-visit the connect login URL — now that we're authenticated via
    # cookie, Zerodha will redirect us through connect/finish → callback?request_token=...
    _token_re = re.compile(r'request_token=([A-Za-z0-9_-]{20,})')
    request_token = None
    current_url = connect_page_url   # kite.zerodha.com/connect/login?api_key=...&v=3

    # Follow redirects until we hit connect/authorize (the React SPA confirm page)
    # or find request_token directly.
    for i in range(6):
        r = session.get(current_url, allow_redirects=False)
        loc = r.headers.get("Location", "")
        logger.info(f"Redirect [{i}]: status={r.status_code} url={current_url[:80]} loc={loc[:80] if loc else 'none'}")

        m = _token_re.search(loc) or _token_re.search(r.url)
        if m:
            request_token = m.group(1)
            break

        if loc:
            if loc.startswith("/"):
                parsed = urlparse(current_url)
                loc = f"{parsed.scheme}://{parsed.netloc}{loc}"
            current_url = loc
        elif r.status_code == 200 and "connect/authorize" in current_url:
            # React SPA — use Playwright to click the Allow button
            logger.info("Authorize page reached — launching headless browser to confirm.")
            request_token = _playwright_authorize(current_url, session.cookies)
            break
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
