"""Application settings, loaded from environment variables (or a .env file)."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "EVE Diagnostics Booking API"

    # SQLite default keeps "clone and run" frictionless; docker-compose uses PostgreSQL.
    database_url: str = "sqlite:///./eve.db"

    jwt_secret: str = "dev-only-jwt-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=60, gt=0)
    bcrypt_rounds: int = Field(default=12, ge=4, le=16)

    # Shared secret used to verify HMAC signatures on incoming payment webhooks.
    webhook_secret: str = "whsec_dev_secret"

    # Probability that the mock provider approves a payment when the client
    # doesn't force an outcome.
    payment_success_rate: float = Field(default=0.8, ge=0.0, le=1.0)

    log_level: str = "INFO"

    login_rate_limit: int = Field(default=5, gt=0)
    login_rate_window_seconds: int = Field(default=60, gt=0)

    # Optional bootstrap admin, created by `python -m app.seed`.
    admin_email: str | None = None
    admin_password: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
