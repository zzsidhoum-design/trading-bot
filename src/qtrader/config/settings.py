"""Application settings — single source of truth, env-driven (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from qtrader.domain.value_objects import TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    qtrader_mode: TradingMode = TradingMode.BACKTEST
    enable_live_trading: bool = False
    log_level: str = "INFO"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "qtrader"
    postgres_user: str = "qtrader"
    postgres_password: str = "qtrader_dev_only"

    redis_url: str = "redis://localhost:6379/0"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str = "change-me"

    # Future phases (declared now so env schema is stable):
    yahoo_enabled: bool = True
    polygon_api_key: str | None = None
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_paper: bool = True
    llm_provider: str = "openai"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    @field_validator("postgres_port")
    @classmethod
    def _port_range(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("postgres_port out of range")
        return value

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def live_enabled(self) -> bool:
        """Live trading requires the mode AND the explicit enable flag."""
        return self.qtrader_mode is TradingMode.LIVE and self.enable_live_trading


@lru_cache
def get_settings() -> Settings:
    return Settings()
