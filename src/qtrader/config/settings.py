"""Application settings — single source of truth, env-driven (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from qtrader.domain.value_objects import Interval, TradingMode


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

    # Phase 2 — data ingestion & scanning
    watchlist: str = "AAPL,MSFT,TSLA"
    data_provider: str = "yahoo"
    backfill_days: int = 30
    quote_cache_ttl_seconds: int = 300
    scan_top_k: int = 20
    scan_lookback_bars: int = 60
    scan_momentum_lookback: int = 20
    scan_min_dollar_volume: float = 500_000.0
    scan_min_atr_pct: float = 0.3

    # Phase 3 — analysis agents
    technical_history_bars: int = 260
    technical_min_bars: int = 60
    news_lookback_hours: int = 24
    news_per_symbol_limit: int = 20
    fundamental_max_age_days: int = 120
    llm_model: str = "gpt-4o-mini"

    # Phase 3+ (declared now so env schema is stable):
    yahoo_enabled: bool = True
    polygon_api_key: str | None = None
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_paper: bool = True
    llm_provider: str = "openai"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    # Phase 4 — prediction & decisions
    prediction_model_name: str = "momentum"
    prediction_horizon: str = "intraday"
    prediction_lookback_bars: int = 120
    prediction_min_bars: int = 30
    train_horizon_bars: int = 12
    train_lookback_bars: int = 120
    train_min_samples: int = 100
    train_promote_threshold: float = 0.52
    decision_weights: str = "technical:0.30,news:0.25,fundamental:0.20,prediction:0.25"
    decision_buy_threshold: float = 0.15
    decision_sell_threshold: float = -0.15
    decision_conflict_threshold: float = 0.5
    decision_min_coverage: float = 0.5

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

    @property
    def watchlist_symbols(self) -> list[str]:
        """Parsed watchlist (comma-separated, trimmed, uppercased)."""
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    @property
    def scan_interval(self) -> Interval:
        """Default intraday interval used by the Market Scanner."""
        return Interval.M5

    @property
    def decision_weights_dict(self) -> dict[str, float]:
        """Parsed per-agent decision weights (e.g. ``technical:0.30,...``)."""
        weights: dict[str, float] = {}
        for part in self.decision_weights.split(","):
            if ":" in part:
                key, value = part.split(":", 1)
                weights[key.strip().lower()] = float(value)
        return weights


@lru_cache
def get_settings() -> Settings:
    return Settings()
