from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # .env.local is where `neon deploy` writes the database variables; later files win
    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), extra="ignore")

    database_url: str = ""
    # Neon's own name for the direct (non-pooled) string is DATABASE_URL_UNPOOLED
    database_url_direct: str = Field("", validation_alias=AliasChoices("DATABASE_URL_DIRECT", "DATABASE_URL_UNPOOLED"))

    cal_username: str = "rohit-singh-g2fz4h"
    cal_event_slug: str = "15min"
    cal_api_key: str = ""
    cal_webhook_secret: str = ""

    jwt_secret: str = ""
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    cors_origins: str = "https://rohitsingh.in,https://www.rohitsingh.in,http://localhost:5179"
    consent_version: str = "privacy-2026-11"
    # "Today" for due dates and overdue payments (the client_summary view uses the same zone)
    business_timezone: str = "Asia/Kolkata"

    firebase_service_account: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
