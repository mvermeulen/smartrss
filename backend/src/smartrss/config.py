"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return [item.strip() for item in value if item.strip()]
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    """Infrastructure settings validated via Pydantic BaseSettings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="postgresql+psycopg://smartrss:smartrss@localhost:5432/smartrss",
        alias="DATABASE_URL",
    )
    lan_port: int = Field(default=8080, alias="LAN_PORT", ge=1, le=65535)
    allowed_hosts_raw: str = Field(
        default="localhost,localhost:8080,127.0.0.1,127.0.0.1:8080",
        alias="ALLOWED_HOSTS",
    )
    cors_allow_origins_raw: str = Field(
        default="http://localhost:8080",
        alias="CORS_ALLOW_ORIGINS",
    )
    ssrf_blocked_cidrs_raw: str = Field(default="", alias="SSRF_BLOCKED_CIDRS")

    worker_mode: Literal["daemon", "cron"] = Field(default="daemon", alias="WORKER_MODE")
    worker_io_concurrency: int = Field(default=4, alias="WORKER_IO_CONCURRENCY", ge=1, le=64)
    worker_poll_interval_seconds: int = Field(
        default=30, alias="WORKER_POLL_INTERVAL_SECONDS", ge=1, le=3600
    )

    # Initial seed defaults used only in Alembic migration 0001
    default_poll_interval_minutes: int = Field(
        default=60, alias="DEFAULT_POLL_INTERVAL_MINUTES", ge=5
    )
    default_backfill_cap: int = Field(default=10, alias="DEFAULT_BACKFILL_CAP", ge=0)
    default_expiration_days: int = Field(default=30, alias="DEFAULT_EXPIRATION_DAYS", ge=0)

    synclog_strict_guard: bool = Field(default=False, alias="SYNCLOG_STRICT_GUARD")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("cors_allow_origins_raw")
    @classmethod
    def _reject_wildcard_cors(cls, value: str) -> str:
        origins = _split_csv(value)
        if any(origin == "*" for origin in origins):
            raise ValueError("Wildcard '*' is forbidden in CORS_ALLOW_ORIGINS")
        return value

    @field_validator("allowed_hosts_raw")
    @classmethod
    def _reject_wildcard_hosts(cls, value: str) -> str:
        hosts = _split_csv(value)
        if any("*" in host for host in hosts):
            raise ValueError("Wildcards are forbidden in ALLOWED_HOSTS")
        return value

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(h.lower() for h in _split_csv(self.allowed_hosts_raw))

    @property
    def cors_allow_origins(self) -> list[str]:
        return _split_csv(self.cors_allow_origins_raw)

    @property
    def ssrf_blocked_cidrs(self) -> list[str]:
        return _split_csv(self.ssrf_blocked_cidrs_raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached process settings."""
    return Settings()
