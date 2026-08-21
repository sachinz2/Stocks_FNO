import os
import secrets

import jwt
from datetime import datetime, timedelta
from fastapi import Header, HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from src.core.config import settings

ALGORITHM = "HS256"
SECRET_KEY = settings.JWT_SECRET
ACCESS_TOKEN_EXPIRE_MINUTES = settings.JWT_EXPIRY_HOURS * 60

security = HTTPBearer()

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


ADMIN_TOKEN_HEADER = "X-Admin-Token"


def require_admin_token(x_admin_token: str = Header(default="", alias=ADMIN_TOKEN_HEADER)) -> None:
    """
    Shared-secret auth for admin/strategy-control/manual-order/manual-signal
    endpoints (admin_router, strategy_router activate/deactivate,
    orders_router POST/DELETE, signals_router /generate).

    Same fail-closed pattern as logs_router._check_token(): reads
    ADMIN_API_TOKEN straight from the environment (set in .env, exactly like
    LOGS_API_TOKEN) on every call, compares with secrets.compare_digest, and
    rejects with 403 if the token is missing/wrong OR if ADMIN_API_TOKEN
    itself isn't configured at all — an empty/unset token must never mean
    "auth disabled, allow everything".

    The JWT-based verify_token()/get_current_user() above is defined but
    unreachable through any real path (nothing ever calls
    create_access_token() to issue a token) -- this shared-secret dependency
    replaces it on the mutating trading-control endpoints instead of trying
    to stand up a full login/session flow.
    """
    expected = os.environ.get("ADMIN_API_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin API is disabled. Set ADMIN_API_TOKEN in .env to enable.",
        )
    if not secrets.compare_digest(x_admin_token or "", expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing admin token.")
