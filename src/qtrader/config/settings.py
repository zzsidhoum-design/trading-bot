"""Application settings — single source of truth, env-driven (pydantic-settings).

Declared as focused mixins grouped by concern (database, API, market data,
analysis, prediction, trading, backtesting, worker) so no single class
becomes a god-object, while all fields keep the flat env schema (``.env``).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from qtrader.domain.value_objects import Interval, TradingMode


class DatabaseSettingsMixin(BaseSettings):
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "qtrader"
    postgres_user: str = "qtrader"
    postgres_password: str = "qtrader_dev_only"

    redis_url: str = "redis://localhost:6379/0"

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


class ApiSettingsMixin(BaseSettings):
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str = "change-me"


class MarketDataSettingsMixin(BaseSettings):
    """Phase 2 — data ingestion & scanning."""

    watchlist: str = "AAPL,MSFT,TSLA"
    data_provider: str = "yahoo"
    backfill_days: int = 30
    backfill_intraday_days: int = 5
    quote_cache_ttl_seconds: int = 300
    data_max_single_bar_move_pct: float = 0.5
    data_reject_large_moves: bool = True
    data_max_calendar_gap_days: int = 10
    scan_top_k: int = 20
    scan_lookback_bars: int = 60
    scan_momentum_lookback: int = 20
    scan_min_dollar_volume: float = 500_000.0
    scan_min_atr_pct: float = 0.3

    @property
    def watchlist_symbols(self) -> list[str]:
        """Parsed watchlist (comma-separated, trimmed, uppercased)."""
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    @property
    def scan_interval(self) -> Interval:
        """Default intraday interval used by the Market Scanner."""
        return Interval.M5


class UniverseSettingsMixin(BaseSettings):
    """Phase 2 — dynamic trading universe engine.

    Every threshold is env-configurable and each value feeds a pure
    classification/selection function so the logic is unit-testable. Tiers are
    strict supersets: ``C`` passes the base liquidity floor, ``B`` is a stricter
    mid tier, ``A`` is the most liquid/large tier. ``None``-able thresholds
    disable that check (e.g. no market-cap floor when the provider has no data).
    """

    universe_refresh_hour: int = 1
    universe_max_candidates: int = 500
    universe_seed_from_watchlist: bool = True
    universe_liquidity_window_days: int = 21
    universe_min_trading_days: int = 30
    universe_min_price: float = 2.0
    universe_min_dollar_volume: float = 1_000_000.0
    universe_min_avg_volume: float = 200_000.0
    universe_max_spread_pct: float | None = 2.0
    universe_min_market_cap: float | None = None
    universe_tier_a_min_dollar_volume: float = 20_000_000.0
    universe_tier_a_min_price: float = 10.0
    universe_tier_b_min_dollar_volume: float = 5_000_000.0
    universe_tier_b_min_price: float = 5.0
    universe_stale_suspend_days: int = 15
    universe_stale_delist_days: int = 60


class AnalysisSettingsMixin(BaseSettings):
    """Phase 3 — analysis agents & external providers."""

    technical_history_bars: int = 260
    technical_min_bars: int = 60
    news_lookback_hours: int = 24
    news_per_symbol_limit: int = 20
    fundamental_max_age_days: int = 120
    llm_model: str = "gpt-4o-mini"

    yahoo_enabled: bool = True
    polygon_api_key: str | None = None
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_paper: bool = True
    llm_provider: str = "openai"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None


class PredictionSettingsMixin(BaseSettings):
    """Phase 4 — prediction & decisions."""

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

    @property
    def decision_weights_dict(self) -> dict[str, float]:
        """Parsed per-agent decision weights (e.g. ``technical:0.30,...``)."""
        weights: dict[str, float] = {}
        for part in self.decision_weights.split(","):
            if ":" in part:
                key, value = part.split(":", 1)
                weights[key.strip().lower()] = float(value)
        return weights


class TradingSettingsMixin(BaseSettings):
    """Phase 5 — risk, portfolio & execution."""

    risk_per_trade_pct: float = 0.01
    max_daily_loss_pct: float = 0.03
    max_portfolio_exposure_pct: float = 0.8
    max_positions: int = 10
    per_sector_limit_pct: float = 0.4
    max_position_pct_adv: float = 0.01
    min_cooldown_minutes: int = 5
    max_trades_per_day: int = 10
    atr_stop_mult: float = 1.5
    take_profit_r_mult: float = 2.0
    risk_allow_add_to_position: bool = False
    allocation_weight_per_trade: float = 0.2
    portfolio_initial_capital: float = 100_000.0
    broker_provider: str = "paper"


class BacktestSettingsMixin(BaseSettings):
    """Phase 6 — backtesting & SystemGate graduation."""

    backtest_interval: str = "1d"
    backtest_universe: str = ""
    backtest_lookback_days: int = 180
    backtest_commission_bps: float = 1.0
    backtest_slippage_bps: float = 0.0
    backtest_warmup_bars: int = 30
    gate_strategy: str = "ensemble"
    gate_oos_strategy: str = ""
    gate_min_trades: int = 30
    gate_min_win_rate: float | None = None
    gate_min_profit_factor: float = 1.2
    gate_min_sharpe: float = 1.0
    gate_max_drawdown: float = 0.25
    gate_min_total_return: float = 0.0
    walk_forward_min_train_samples: int = 50
    walk_forward_folds: int = 5
    walk_forward_lookback_bars: int = 60
    walk_forward_horizon_bars: int = 12
    walk_forward_prob_buy: float = 0.52
    walk_forward_prob_sell: float = 0.48


class MarketSettingsMixin(BaseSettings):
    """Trading-session calendar — the pipeline only trades when the exchange is open."""

    market_timezone: str = "America/New_York"
    market_open: str = "09:30"
    market_close: str = "16:00"
    market_holidays: str = ""
    market_always_open: bool = False

    @property
    def market_hours(self) -> Any:
        """A :class:`~qtrader.application.services.market_hours.MarketHours`.

        Extra ``market_holidays`` (comma-separated ``YYYY-MM-DD``) are added to
        the curated NYSE/NASDAQ calendar. ``market_always_open`` disables the
        calendar entirely (backtests, CI).
        """
        from qtrader.application.services.market_hours import MarketHours

        extras = [d.strip() for d in self.market_holidays.split(",") if d.strip()]
        return MarketHours(
            timezone=self.market_timezone,
            open_time=self.market_open,
            close_time=self.market_close,
            holidays=extras,
            always_open=self.market_always_open,
        )


class WorkerSettingsMixin(BaseSettings):
    """Phase 8 — hardening: resilience + worker sharding."""

    provider_failure_threshold: int = 5
    provider_reset_timeout_seconds: float = 30.0
    worker_shards: int = 1
    worker_shard_id: int = 0

    @field_validator("worker_shard_id")
    @classmethod
    def _shard_in_range(cls, value: int, info: Any) -> int:
        shards = info.data.get("worker_shards", 1)
        if not 0 <= value < max(shards, 1):
            raise ValueError("worker_shard_id must be in [0, worker_shards)")
        return value


class ResearchSettingsMixin(BaseSettings):
    """Phase 3 — multi-timeframe research engine (no strategies, research only)."""

    research_lookback_days: int = 730
    research_min_train_bars: int = 100
    research_min_coverage_pct: float = 0.9
    research_combination_mode: str = "all"
    research_max_symbols: int = 20
    research_n_folds: int = 4
    research_signal_mode: str = "trend"
    research_signal_fast: int = 9
    research_signal_slow: int = 21
    research_signal_band: float = 0.0
    research_commission_bps: float = 10.0
    research_slippage_bps: float = 50.0
    research_max_hold_bars: int = 0
    research_intervals: str = ""

    @property
    def research_settings(self) -> Any:
        """A :class:`~qtrader.application.services.multitimeframe.ResearchSettings`."""
        from qtrader.application.services.multitimeframe import (
            ResearchSettings,
            SignalParams,
            SimParams,
        )

        def _parse_interval(token: str) -> Interval:
            """Accept either the enum name (``D1``) or its value (``1d``)."""
            t = token.strip().upper()
            for iv in Interval:
                if iv.name == t or iv.value.upper() == t:
                    return iv
            raise ValueError(f"unknown research interval {token!r}")

        intervals: tuple[Interval, ...] | None = None
        if self.research_intervals.strip():
            parts = [p for p in self.research_intervals.split(",") if p.strip()]
            intervals = tuple(_parse_interval(p) for p in parts)
        return ResearchSettings(
            intervals=intervals or ResearchSettings().intervals,
            lookback_days=self.research_lookback_days,
            signal=SignalParams(
                mode=self.research_signal_mode,
                fast=self.research_signal_fast,
                slow=self.research_signal_slow,
                band=self.research_signal_band,
            ),
            sim=SimParams(
                commission_bps=self.research_commission_bps,
                slippage_bps=self.research_slippage_bps,
                max_hold_bars=self.research_max_hold_bars,
            ),
            n_folds=self.research_n_folds,
            min_train_bars=self.research_min_train_bars,
            min_coverage_pct=self.research_min_coverage_pct,
            combination_mode=self.research_combination_mode,
            max_symbols=self.research_max_symbols,
        )


class StrategyResearchSettingsMixin(BaseSettings):
    """Phase 2 — automated strategy research engine (research only, no trading)."""

    strategy_research_max_strategies: int = 60
    strategy_research_computational_budget: int = 60
    strategy_research_max_indicators: int = 5
    strategy_research_max_conditions: int = 3
    strategy_research_intervals: str = ""
    strategy_research_commission_bps: float = 10.0
    strategy_research_slippage_bps: float = 50.0
    strategy_research_initial_capital: float = 100_000.0
    strategy_research_min_trades: int = 30
    strategy_research_min_sharpe: float = 0.0
    strategy_research_instability_budget: int = 12
    strategy_research_regime_gate: bool = True

    @property
    def strategy_research_plan(self) -> Any:
        """A :class:`~qtrader.application.research.strategy.engine.ResearchPlan`."""
        from decimal import Decimal

        from qtrader.application.research.strategy.engine import MetricGate, ResearchPlan
        from qtrader.application.research.strategy.generator import SearchLimits

        def _parse_interval(token: str) -> Interval:
            """Accept either the enum name (``D1``) or its value (``1d``)."""
            t = token.strip().upper()
            for iv in Interval:
                if iv.name == t or iv.value.upper() == t:
                    return iv
            raise ValueError(f"unknown research interval {token!r}")

        intervals: tuple[Interval, ...] = (Interval.D1,)
        if self.strategy_research_intervals.strip():
            parts = [p for p in self.strategy_research_intervals.split(",") if p.strip()]
            intervals = tuple(_parse_interval(p) for p in parts)
        return ResearchPlan(
            limits=SearchLimits(
                max_strategies=self.strategy_research_max_strategies,
                computational_budget=self.strategy_research_computational_budget,
                max_indicators=self.strategy_research_max_indicators,
                max_conditions=self.strategy_research_max_conditions,
                intervals=intervals,
            ),
            gate=MetricGate(
                min_sharpe=self.strategy_research_min_sharpe,
                min_trades=self.strategy_research_min_trades,
            ),
            initial_capital=Decimal(str(self.strategy_research_initial_capital)),
            commission_bps=self.strategy_research_commission_bps,
            slippage_bps=self.strategy_research_slippage_bps,
            instability_budget=self.strategy_research_instability_budget,
        )


class Settings(
    DatabaseSettingsMixin,
    ApiSettingsMixin,
    MarketDataSettingsMixin,
    UniverseSettingsMixin,
    AnalysisSettingsMixin,
    PredictionSettingsMixin,
    TradingSettingsMixin,
    BacktestSettingsMixin,
    MarketSettingsMixin,
    WorkerSettingsMixin,
    ResearchSettingsMixin,
    StrategyResearchSettingsMixin,
    BaseSettings,
):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    qtrader_mode: TradingMode = TradingMode.BACKTEST
    enable_live_trading: bool = False
    log_level: str = "INFO"

    @property
    def live_enabled(self) -> bool:
        """Live trading requires the mode AND the explicit enable flag."""
        return self.qtrader_mode is TradingMode.LIVE and self.enable_live_trading


@lru_cache
def get_settings() -> Settings:
    return Settings()
