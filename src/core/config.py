from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Falcon Quant Platform"
    ENV: str = "development"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # Database (MySQL)
    DB_HOST: str = "mysql"
    DB_PORT: int = 3306
    DB_NAME: str = "falcon_db"
    DB_USER: str = "falcon_user"
    DB_PASSWORD: str = "password123"
    DATABASE_URL: str = ""

    # Redis
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""

    # JWT
    JWT_SECRET: str = "change-me-in-production"
    JWT_EXPIRY_HOURS: int = 24

    # Zerodha Kite Connect
    ZERODHA_API_KEY: str = ""
    ZERODHA_API_SECRET: str = ""
    ZERODHA_USER_ID: str = ""
    ZERODHA_PASSWORD: str = ""
    ZERODHA_TOTP_SECRET: str = ""

    # Telegram (kept for reference, no longer used)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Email alerts (Gmail SMTP)
    EMAIL_SENDER: str = ""
    EMAIL_APP_PASSWORD: str = ""
    EMAIL_RECIPIENT: str = ""

    # Trading
    INITIAL_CAPITAL: float = 300000.0
    MAX_OPEN_POSITIONS: int = 5
    MAX_DAILY_LOSS_PCT: float = 0.05
    MAX_EXPOSURE_PCT: float = 0.30
    TRADING_MODE: str = "paper"  # paper | live

    # Dashboard
    DASHBOARD_PASSWORD: str = "falcon123"

    # Logs API
    LOGS_API_TOKEN: str = ""   # set in .env — empty = logs endpoint disabled

    # extra="ignore": a legacy/unrelated env var in .env (e.g. a leftover
    # REDIS_URL from an older config generation -- this class only ever
    # reads REDIS_HOST/PORT/PASSWORD and derives the URL itself via
    # get_redis_url()) must not crash the entire application at import
    # time. Found 2026-08-07: pydantic-settings' default extra="forbid"
    # meant Settings() -- and therefore every module that imports it,
    # including live_trading_engine.py -- couldn't even be imported in a
    # dev environment whose .env carried that one stray key.
    model_config = {
        "env_file": ".env", "env_file_encoding": "utf-8",
        "case_sensitive": True, "extra": "ignore",
    }

    def get_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        from urllib.parse import quote_plus
        return f"mysql+aiomysql://{self.DB_USER}:{quote_plus(self.DB_PASSWORD)}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    def get_redis_url(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Known-insecure fallback values baked into the class defaults above -- fine
# for local dev (no .env present at all), dangerous if they're still active
# in a real deployment. Checked by validate_production_secrets() at startup
# rather than here at import time, so importing this module (e.g. in tests,
# scripts, or a dev shell) never has the side effect of raising.
_INSECURE_DEFAULTS = {
    "DB_PASSWORD":        "password123",
    "JWT_SECRET":          "change-me-in-production",
    "DASHBOARD_PASSWORD":  "falcon123",
}


def validate_production_secrets(s: Settings = settings) -> None:
    """
    Fail fast at startup if ENV=production and any credential is still the
    known-insecure hardcoded default (external review, 2026-08-20 -- found
    DB_PASSWORD literally still "password123" on the live server, sitting
    in a public GitHub repo as this class's own fallback value). A
    misconfigured or incomplete .env should refuse to boot in production
    rather than silently run with a publicly-known credential.
    """
    if s.ENV != "production":
        return
    still_default = [
        name for name, default_value in _INSECURE_DEFAULTS.items()
        if getattr(s, name) == default_value
    ]
    if still_default:
        raise RuntimeError(
            f"FATAL: ENV=production but these settings are still their known-"
            f"insecure hardcoded defaults: {', '.join(still_default)}. Set real "
            f"values in .env before starting in production."
        )
