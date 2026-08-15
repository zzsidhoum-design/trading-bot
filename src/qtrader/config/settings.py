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


class PortfolioRiskSettingsMixin(BaseSettings):
    """Phase 5 — the independent Portfolio & Risk Management Engine.

    All controls are configurable and default conservative. The engine is
    authoritative and independent of the AI agents and the Phase 6 AI
    Strategy Selector.
    """

    # Portfolio constraints (fractions unless stated otherwise).
    pr_max_position_weight_pct: float = 0.25
    pr_max_portfolio_exposure_pct: float = 0.80
    pr_max_sector_exposure_pct: float = 0.40
    pr_max_correlated_exposure_pct: float = 0.50
    pr_correlation_threshold: float = 0.70
    pr_max_positions: int = 10
    pr_max_turnover_pct: float = 0.50
    pr_max_leverage_pct: float = 0.0

    # Position sizing.
    pr_sizing_method: str = "fixed_allocation"
    pr_fixed_allocation_pct: float = 0.20
    pr_vol_target_pct: float = 0.10
    pr_max_annualized_vol_pct: float = 0.40
    pr_risk_per_trade_pct: float = 0.01
    pr_max_weight_pct: float = 0.25
    pr_annualization: float = 252.0

    # Drawdown protection / failure controls.
    pr_max_strategy_drawdown_pct: float = 0.25
    pr_max_portfolio_drawdown_pct: float = 0.20
    pr_max_daily_loss_pct: float = 0.03
    pr_max_consecutive_losses: int = 5
    pr_monitor_drawdown_pct: float = 0.15
    pr_reduce_drawdown_pct: float = 0.20
    pr_suspension_cooldown_days: int = 30
    pr_monitored_weight_factor: float = 0.75
    pr_reduced_weight_factor: float = 0.50

    # Risk-aware strategy allocation weights.
    pr_allocation_sharpe_weight: float = 1.0
    pr_allocation_sortino_weight: float = 0.5
    pr_allocation_oos_return_weight: float = 0.5
    pr_allocation_execution_weight: float = 1.0
    pr_allocation_drawdown_weight: float = 1.0
    pr_allocation_volatility_weight: float = 0.5
    pr_allocation_correlation_weight: float = 1.0
    pr_allocation_regime_weight: float = 0.0
    pr_allocation_min_weight_pct: float = 0.05
    pr_allocation_max_weight_pct: float = 0.50

    @property
    def portfolio_risk_plan(self) -> Any:
        """A :class:`~qtrader.application.portfolio_mgmt.models.PortfolioRiskPlan`."""
        from qtrader.application.portfolio_mgmt.models import (
            AllocationPolicyConfig,
            DrawdownProtection,
            PortfolioConstraints,
            PortfolioRiskPlan,
            PositionSizingMethod,
            SizingPolicy,
        )

        return PortfolioRiskPlan(
            constraints=PortfolioConstraints(
                max_position_weight_pct=self.pr_max_position_weight_pct,
                max_portfolio_exposure_pct=self.pr_max_portfolio_exposure_pct,
                max_sector_exposure_pct=self.pr_max_sector_exposure_pct,
                max_correlated_exposure_pct=self.pr_max_correlated_exposure_pct,
                correlation_threshold=self.pr_correlation_threshold,
                max_positions=self.pr_max_positions,
                max_turnover_pct=self.pr_max_turnover_pct,
                max_leverage_pct=self.pr_max_leverage_pct,
            ),
            drawdown_protection=DrawdownProtection(
                max_strategy_drawdown_pct=self.pr_max_strategy_drawdown_pct,
                max_portfolio_drawdown_pct=self.pr_max_portfolio_drawdown_pct,
                max_daily_loss_pct=self.pr_max_daily_loss_pct,
                max_consecutive_losses=self.pr_max_consecutive_losses,
                monitor_drawdown_pct=self.pr_monitor_drawdown_pct,
                reduce_drawdown_pct=self.pr_reduce_drawdown_pct,
                suspension_cooldown_days=self.pr_suspension_cooldown_days,
                monitored_weight_factor=self.pr_monitored_weight_factor,
                reduced_weight_factor=self.pr_reduced_weight_factor,
            ),
            sizing=SizingPolicy(
                method=PositionSizingMethod(self.pr_sizing_method),
                fixed_allocation_pct=self.pr_fixed_allocation_pct,
                vol_target_pct=self.pr_vol_target_pct,
                max_annualized_vol_pct=self.pr_max_annualized_vol_pct,
                risk_per_trade_pct=self.pr_risk_per_trade_pct,
                max_weight_pct=self.pr_max_weight_pct,
                annualization=self.pr_annualization,
            ),
            allocation=AllocationPolicyConfig(
                sharpe_weight=self.pr_allocation_sharpe_weight,
                sortino_weight=self.pr_allocation_sortino_weight,
                oos_return_weight=self.pr_allocation_oos_return_weight,
                execution_weight=self.pr_allocation_execution_weight,
                drawdown_weight=self.pr_allocation_drawdown_weight,
                volatility_weight=self.pr_allocation_volatility_weight,
                correlation_weight=self.pr_allocation_correlation_weight,
                regime_weight=self.pr_allocation_regime_weight,
                min_weight_pct=self.pr_allocation_min_weight_pct,
                max_weight_pct=self.pr_allocation_max_weight_pct,
            ),
        )


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


class StrategyValidationSettingsMixin(BaseSettings):
    """Phase 3 — automated strategy validation & edge detection (research only)."""

    strategy_validation_max_strategies: int = 60
    strategy_validation_computational_budget: int = 60
    strategy_validation_max_indicators: int = 5
    strategy_validation_max_conditions: int = 3
    strategy_validation_intervals: str = ""
    strategy_validation_commission_bps: float = 10.0
    strategy_validation_slippage_bps: float = 50.0
    strategy_validation_initial_capital: float = 100_000.0
    strategy_validation_dev_fraction: float = 0.5
    strategy_validation_validation_fraction: float = 0.25
    strategy_validation_folds: int = 4
    strategy_validation_lookback_bars: int = 60
    strategy_validation_horizon_bars: int = 12
    strategy_validation_warmup_bars: int = 30
    strategy_validation_benchmark_gate: bool = True
    strategy_validation_max_ranked: int = 10
    strategy_validation_min_trades: int = 30
    strategy_validation_min_sharpe: float = 0.0
    strategy_validation_max_drawdown: float = -0.5

    @property
    def strategy_validation_plan(self) -> Any:
        """A :class:`~qtrader.application.research.validation.records.ValidationPlan`."""
        from decimal import Decimal

        from qtrader.application.research.strategy.engine import MetricGate
        from qtrader.application.research.strategy.generator import SearchLimits
        from qtrader.application.research.validation.filters import InitialFilterLimits
        from qtrader.application.research.validation.records import ValidationPlan

        def _parse_interval(token: str) -> Interval:
            """Accept either the enum name (``D1``) or its value (``1d``)."""
            t = token.strip().upper()
            for iv in Interval:
                if iv.name == t or iv.value.upper() == t:
                    return iv
            raise ValueError(f"unknown validation interval {token!r}")

        intervals: tuple[Interval, ...] = (Interval.D1,)
        if self.strategy_validation_intervals.strip():
            parts = [p for p in self.strategy_validation_intervals.split(",") if p.strip()]
            intervals = tuple(_parse_interval(p) for p in parts)
        return ValidationPlan(
            limits=SearchLimits(
                max_strategies=self.strategy_validation_max_strategies,
                computational_budget=self.strategy_validation_computational_budget,
                max_indicators=self.strategy_validation_max_indicators,
                max_conditions=self.strategy_validation_max_conditions,
                intervals=intervals,
            ),
            initial_filter=InitialFilterLimits(
                min_trades=self.strategy_validation_min_trades,
            ),
            dev_gate=MetricGate(
                min_sharpe=self.strategy_validation_min_sharpe,
                min_trades=self.strategy_validation_min_trades,
            ),
            wf_gate=MetricGate(
                min_sharpe=self.strategy_validation_min_sharpe,
                min_trades=self.strategy_validation_min_trades,
            ),
            oos_gate=MetricGate(
                min_sharpe=self.strategy_validation_min_sharpe,
                min_trades=self.strategy_validation_min_trades,
                max_drawdown=self.strategy_validation_max_drawdown,
            ),
            dev_fraction=self.strategy_validation_dev_fraction,
            validation_fraction=self.strategy_validation_validation_fraction,
            initial_capital=Decimal(str(self.strategy_validation_initial_capital)),
            commission_bps=self.strategy_validation_commission_bps,
            slippage_bps=self.strategy_validation_slippage_bps,
            warmup_bars=self.strategy_validation_warmup_bars,
            folds=self.strategy_validation_folds,
            lookback_bars=self.strategy_validation_lookback_bars,
            horizon_bars=self.strategy_validation_horizon_bars,
            intervals=intervals,
            benchmark_gate=self.strategy_validation_benchmark_gate,
            max_ranked=self.strategy_validation_max_ranked,
        )


class StrategyExecutionSettingsMixin(BaseSettings):
    """Phase 4 — execution-robustness verdicts on validated strategies."""

    strategy_execution_commission_bps: float = 10.0
    strategy_execution_min_fill_rate: float = 0.90
    strategy_execution_min_net_sharpe: float = 0.0
    strategy_execution_max_sharpe_degradation: float = 0.5
    strategy_execution_max_return_degradation: float = 0.5
    strategy_execution_max_rejected_rate: float = 0.25
    strategy_execution_min_avg_volume: float = 50_000.0
    strategy_execution_min_avg_dollar_volume: float = 500_000.0
    strategy_execution_seed: int = 42

    @property
    def strategy_execution_plan(self) -> Any:
        """A :class:`~qtrader.application.execution.models.ExecutionPlan`."""
        from decimal import Decimal

        from qtrader.application.execution.models import (
            ExecutionPlan,
            LiquidityAssumptions,
        )

        return ExecutionPlan(
            commission_bps=self.strategy_execution_commission_bps,
            min_fill_rate=self.strategy_execution_min_fill_rate,
            min_net_sharpe=self.strategy_execution_min_net_sharpe,
            max_absolute_sharpe_degradation=self.strategy_execution_max_sharpe_degradation,
            max_return_degradation=self.strategy_execution_max_return_degradation,
            max_rejected_rate=self.strategy_execution_max_rejected_rate,
            liquidity=LiquidityAssumptions(
                min_avg_volume=Decimal(str(self.strategy_execution_min_avg_volume)),
                min_avg_dollar_volume=Decimal(
                    str(self.strategy_execution_min_avg_dollar_volume)
                ),
            ),
            seed=self.strategy_execution_seed,
        )


class PaperSettingsMixin(BaseSettings):
    """Phase 7 — paper trading & shadow deployment.

    ``paper_shadow_mode`` runs the full decision pipeline in shadow mode: every
    decision is recorded but no paper order is ever submitted. Acceptance
    thresholds are operational (fill rate, slippage, latency, drawdown,
    divergence, data reliability, failure rate) — deliberately not profit-based.
    """

    paper_ledger_path: str = "data/paper/orders.jsonl"
    paper_default_price: float = 100.0
    paper_shadow_mode: bool = False
    paper_telemetry_enabled: bool = True
    paper_window_days: int = 30
    paper_accept_min_fill_rate: float = 0.90
    paper_accept_max_slippage_bps: float = 50.0
    paper_accept_max_avg_latency_ms: float = 5000.0
    paper_accept_max_drawdown: float = 0.20
    paper_accept_max_divergence: float = 0.10
    paper_accept_min_data_reliability: float = 0.95
    paper_accept_max_failure_rate: float = 0.05

    @property
    def paper_acceptance_thresholds(self) -> Any:
        """A :class:`~qtrader.application.paper.acceptance.AcceptanceThresholds`."""
        from qtrader.application.paper.acceptance import AcceptanceThresholds

        return AcceptanceThresholds(
            min_fill_rate=self.paper_accept_min_fill_rate,
            max_slippage_bps=self.paper_accept_max_slippage_bps,
            max_avg_latency_ms=self.paper_accept_max_avg_latency_ms,
            max_drawdown=self.paper_accept_max_drawdown,
            max_paper_research_divergence=self.paper_accept_max_divergence,
            min_data_reliability=self.paper_accept_min_data_reliability,
            max_failure_rate=self.paper_accept_max_failure_rate,
        )


class AiSettingsMixin(BaseSettings):
    """Phase 6 — AI Strategy Selection & Multi-Agent Integration (research).

    Everything the AI layer needs is configurable and defaults conservative.
    Agent weights are versioned so any change is auditable; the news sentiment
    model is either the offline ``lexicon`` (deterministic fallback) or the
    lazy ``finbert`` wrapper (requires ``transformers``). Live trading remains
    disabled by ``enable_live_trading`` at the application level.
    """

    ai_weights_version: str = "1.0"
    ai_enabled_agents: str = (
        "technical,news,fundamental,pattern,prediction,regime"
    )
    ai_agent_weights: str = (
        "technical:1.0,news:0.8,fundamental:0.6,pattern:0.5,"
        "prediction:0.7,regime:0.4"
    )

    ai_min_ensemble_abs_score: float = 0.15
    ai_min_confidence: float = 0.0
    ai_min_agreeing_agents: int = 1
    ai_position_size_pct: float = 0.02
    ai_leverage: float = 1.0

    ai_news_model: str = "lexicon"
    ai_finbert_model_name: str = "ProsusAI/finbert"
    ai_news_lookback_hours: int = 24
    ai_news_per_symbol_limit: int = 20

    ai_execution_scenario: str = "baseline"
    ai_execution_commission_bps: float = 10.0
    ai_execution_max_participation_rate: float = 0.10
    ai_execution_seed: int = 42

    ai_ablation_risk_free_rate: float = 0.0
    ai_ablation_periods_per_year: float = 252.0

    ai_failure_max_agent_dispersion: float = 1.0
    ai_failure_max_mean_confidence: float = 0.95
    ai_failure_max_confidence_std: float = 0.25
    ai_failure_news_staleness_hours: float = 48.0
    ai_failure_drift_max_abs: float = 0.50
    ai_failure_drift_history: int = 100

    ai_ledger_path: str = "data/ai/decisions.jsonl"

    @property
    def ai_enabled_agent_list(self) -> list[str]:
        """Parsed enabled agents (comma-separated, trimmed)."""
        return [a.strip() for a in self.ai_enabled_agents.split(",") if a.strip()]

    @property
    def ai_weights_config(self) -> Any:
        """A versioned :class:`~qtrader.application.ai.models.AgentWeightsConfig`."""
        from qtrader.application.ai.models import AgentWeightsConfig

        weights: dict[str, float] = {}
        for token in self.ai_agent_weights.split(","):
            token = token.strip()
            if not token:
                continue
            if ":" in token:
                agent, value = token.split(":", 1)
                weights[agent.strip()] = float(value.strip())
            else:
                weights[token] = 1.0
        enabled = tuple(self.ai_enabled_agent_list)
        return AgentWeightsConfig(
            version=self.ai_weights_version,
            weights=weights,
            enabled=enabled or (),
        )

    @property
    def ai_decision_config(self) -> Any:
        """A :class:`~qtrader.application.ai.decision.DecisionConfig`."""
        from qtrader.application.ai.decision import DecisionConfig

        return DecisionConfig(
            min_ensemble_abs_score=self.ai_min_ensemble_abs_score,
            min_confidence=self.ai_min_confidence,
            min_agreeing_agents=self.ai_min_agreeing_agents,
            position_size_pct=self.ai_position_size_pct,
            leverage=self.ai_leverage,
        )

    @property
    def ai_failure_config(self) -> Any:
        """A :class:`~qtrader.application.ai.failure.FailureConfig`."""
        from qtrader.application.ai.failure import FailureConfig

        return FailureConfig(
            max_agent_dispersion=self.ai_failure_max_agent_dispersion,
            max_mean_confidence=self.ai_failure_max_mean_confidence,
            max_confidence_std=self.ai_failure_max_confidence_std,
            news_staleness_hours=self.ai_failure_news_staleness_hours,
            drift_max_abs=self.ai_failure_drift_max_abs,
            drift_history=self.ai_failure_drift_history,
        )

    @property
    def ai_selector_config(self) -> Any:
        """A :class:`~qtrader.application.ai.selector.SelectorConfig`."""
        from qtrader.application.ai.selector import SelectorConfig

        return SelectorConfig()


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
    StrategyValidationSettingsMixin,
    StrategyExecutionSettingsMixin,
    PortfolioRiskSettingsMixin,
    AiSettingsMixin,
    PaperSettingsMixin,
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
