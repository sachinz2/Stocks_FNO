"""
Zerodha Daily Authentication Script
Run this every morning before 9:15 AM IST to get a fresh access token.

Usage:
    python scripts/zerodha_auth.py
"""
import os
import sys
import json
import redis
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kiteconnect import KiteConnect
from src.core.config import settings

REDIS_KEY = "zerodha:access_token"


def get_redis():
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
    )


def main():
    api_key = settings.ZERODHA_API_KEY
    api_secret = settings.ZERODHA_API_SECRET

    if not api_key or not api_secret:
        print("ERROR: ZERODHA_API_KEY and ZERODHA_API_SECRET must be set in .env")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)

    # Step 1 — Show login URL
    login_url = kite.login_url()
    print("\n" + "=" * 60)
    print("  ZERODHA DAILY LOGIN")
    print("=" * 60)
    print("\nStep 1: Open this URL in your browser:\n")
    print(f"  {login_url}\n")
    print("Step 2: Login with your Zerodha credentials + OTP")
    print("Step 3: After login, you'll be redirected to localhost.")
    print("        The URL will look like:")
    print("        http://localhost?request_token=XXXXXXXX&action=login&status=success")
    print("\nStep 4: Copy the request_token value from that URL.")
    print("=" * 60)

    request_token = input("\nPaste the request_token here: ").strip()

    if not request_token:
        print("ERROR: No token provided.")
        sys.exit(1)

    # Step 2 — Exchange for access token
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data["access_token"]
        user_id = data.get("user_id", "")
        print(f"\nAuthenticated as: {user_id}")
    except Exception as e:
        print(f"\nERROR: Failed to generate session: {e}")
        sys.exit(1)

    # Step 3 — Store in Redis (expires at midnight)
    try:
        r = get_redis()
        r.set(REDIS_KEY, access_token, ex=86400)  # 24h TTL
        print(f"Access token stored in Redis (key: {REDIS_KEY})")
    except Exception as e:
        print(f"WARNING: Could not store in Redis: {e}")

    # Step 4 — Also save to file as backup
    token_file = os.path.join(os.path.dirname(__file__), ".zerodha_token")
    with open(token_file, "w") as f:
        json.dump({
            "access_token": access_token,
            "user_id": user_id,
            "generated_at": datetime.now().isoformat(),
        }, f)
    print(f"Access token saved to: {token_file}")

    print("\n" + "=" * 60)
    print("  LOGIN SUCCESSFUL — Ready for trading")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
