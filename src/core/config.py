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

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Trading
    INITIAL_CAPITAL: float = 300000.0
    MAX_OPEN_POSITIONS: int = 5
    MAX_DAILY_LOSS_PCT: float = 0.05
    MAX_EXPOSURE_PCT: float = 0.20
    TRADING_MODE: str = "paper"  # paper | live

    # Dashboard
    DASHBOARD_PASSWORD: str = "falcon123"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "case_sensitive": True}

    def get_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    def get_redis_url(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
