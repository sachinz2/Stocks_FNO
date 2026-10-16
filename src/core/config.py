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
    # Topped up 300,000 -> 600,000 (2026-10-16, explicit user instruction:
    # "we want trades in iron_condor... what more can we do?"). Real blocker
    # found live: PaperBroker.balance had shrunk to ~Rs1,07,515 from
    # cumulative realized paper losses across the gate-loosening experiment,
    # and _check_available_margin() (paper mode) checks THIS balance minus
    # capital locked in active spreads/condors -- not the per-strategy soft
    # budget in risk_manager.py. Most iron_condor_v1 candidates this morning
    # (COALINDIA, PIDILITIND, SBIN, TITAN, LT, INDUSINDBK, DMART) were
    # rejected "insufficient margin" needing Rs27k-60k each against ~Rs40k
    # actually available. _reconcile_paper_broker_balance() recomputes
    # broker.balance as INITIAL_CAPITAL + total_realized_pnl - deployed_cash
    # on every restart, so this top-up takes effect on the next restart
    # without wiping any trade history.
    #
    # Topped up AGAIN 600,000 -> 1,500,000 (same day, ~2hrs later): the
    # 600K top-up fixed _check_available_margin() (the entry gate) but
    # iron_condor_v1 candidates then started clearing every gate and still
    # failing at actual order placement -- PaperBroker.place_order() has
    # its OWN, much stricter, independent margin model for SELL-to-open
    # legs (see SHORT_MARGIN_MULTIPLE_OF_PREMIUM in paper_broker.py: 10x
    # the premium per short lot, not wing-width-based). The 3 existing
    # credit_spread_v1 short legs (HDFCBANK, POWERGRID, WIPRO) alone
    # already lock ~Rs2,98,930 under that model, leaving only ~Rs16-28K
    # free -- nowhere near the Rs55k-233k iron_condor_v1 candidates needed.
    # User chose "top up again" over loosening SHORT_MARGIN_MULTIPLE_OF_PREMIUM
    # itself (which would reduce modeled short-selling risk for ALL short
    # legs, not just unblock capital). Restore 300,000 (or reconsider
    # entirely) after reviewing case-by-case losses.
    INITIAL_CAPITAL: float = 1500000.0
    MAX_OPEN_POSITIONS: int = 5
    # Reverted back to 0.05 (2026-10-09): the earlier same-day 0.05->0.20
    # resize (a6be7e7) is superseded by a full, date-bounded bypass of the
    # daily-loss check itself in risk_manager.validate_trade() -- "I asked
    # you to deactivate the kill switch for 2 weeks." This value only
    # matters again once that bypass expires (2026-10-23), so it resumes at
    # the original conservative default rather than a still-loosened one.
    MAX_DAILY_LOSS_PCT: float = 0.05
    MAX_EXPOSURE_PCT: float = 0.30
    TRADING_MODE: str = "paper"  # paper | live

    # Dashboard
    DASHBOARD_PASSWORD: str = "falcon123"

    # Logs API
    LOGS_API_TOKEN: str = ""   # set in .env — empty = logs endpoint disabled

    # Admin / trading-control API (admin_router, strategy activate/deactivate,
    # manual orders, manual signal generation) — shared secret checked via
    # require_admin_token() in src/api/services/auth.py. Same fail-closed
    # convention as LOGS_API_TOKEN: empty = endpoints disabled (403), not open.
    ADMIN_API_TOKEN: str = ""

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
