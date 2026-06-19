"""
Zerodha connection diagnostic — run inside the container:
    docker exec -it falcon_api python scripts/diagnose_zerodha.py
"""
import os
import threading

import redis as sync_redis
from kiteconnect import KiteConnect, KiteTicker

api_key  = os.environ["ZERODHA_API_KEY"]
redis_pw = os.environ.get("REDIS_PASSWORD", "")

r     = sync_redis.Redis(host="falcon_redis", port=6379, password=redis_pw, decode_responses=True)
token = r.get("zerodha:access_token")

print("=" * 50)
print(f"API Key : {api_key}")
print(f"Token   : {token[:16]}..." if token else "Token   : MISSING — run zerodha_auto_auth.py first")
print("=" * 50)

if not token:
    raise SystemExit(1)

# ── Test 1: REST API ──────────────────────────────────
print("\n[1] Testing REST API...")
kite = KiteConnect(api_key=api_key)
kite.set_access_token(token)
try:
    p = kite.profile()
    print(f"    REST OK : {p['user_id']} — {p['user_name']} ({p['email']})")
except Exception as e:
    print(f"    REST FAIL: {e}")

# ── Test 2: WebSocket ─────────────────────────────────
print("\n[2] Testing WebSocket...")
done = threading.Event()

def on_connect(ws, response):
    print("    WS OK   : WebSocket connected successfully!")
    done.set()
    ws.close()

def on_error(ws, code, reason):
    print(f"    WS FAIL : code={code}  reason={reason}")
    done.set()

def on_close(ws, code, reason):
    done.set()

kws = KiteTicker(api_key, token, reconnect=False)
kws.on_connect    = on_connect
kws.on_error      = on_error
kws.on_close      = on_close
kws.connect(threaded=True)

if not done.wait(timeout=10):
    print("    WS FAIL : No response after 10 seconds (timeout)")

print("\nDone.")
