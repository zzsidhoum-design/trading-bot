"""Scheduled worker tasks (arq).

Each task receives ``ctx`` with ``redis`` (arq's connection) and any kwargs
from the cron entry. Tasks must be idempotent — arq guarantees at-least-once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from qtrader.config.settings import Settings
from qtrader.domain.value_objects import Interval


def _owned(symbols: list[str]) -> list[str]:
    """Filter ``symbols`` down to this worker's shard (see application/services/shard.py)."""
    from qtrader.application.services.shard import owned_symbols

    settings = Settings()
    return owned_symbols(symbols, settings.worker_shard_id, settings.worker_shards)


async def heartbeat(ctx: dict[str, Any]) -> str:
    """Prove the worker is alive: round-trip through the shared cache/DB."""
    from redis.asyncio import Redis

    from qtrader.config.container import get_container
    from qtrader.infrastructure.cache import RedisCache

    container = get_container()
    cache = RedisCache(container.resolve(Redis))
    await cache.set("worker:heartbeat", "1", ttl_seconds=300)
    db_ok = await container.database_healthy()
    return f"heartbeat db={db_ok}"


async def backfill(
    ctx: dict[str, Any],
    symbol: str | None = None,
    interval: str | None = None,
    days: int | None = None,
) -> str:
    """Data Agent job: pull clean history for the watchlist (or one symbol)."""
    from qtrader.application.agents.data import DataAgent
    from qtrader.config.container import get_container

    container = get_container()
    settings = container.resolve(Settings)
    agent = container.resolve(DataAgent)
    iv = Interval(interval) if interval else settings.scan_interval
    symbols = [symbol] if symbol else settings.watchlist_symbols
    symbols = _owned(symbols)
    end = datetime.now(UTC)
    start = end - timedelta(days=days or settings.backfill_days)
    total = 0
    for sym in symbols:
        inserted = await agent.backfill(sym, iv, start, end)
        total += inserted
    return f"backfilled {total} bars for {len(symbols)} symbols ({iv})"


async def scan_cycle(ctx: dict[str, Any]) -> str:
    """Market Scanner cycle: recompute top-K rankings."""
    from qtrader.application.agents.scanner import MarketScanner
    from qtrader.config.container import get_container

    container = get_container()
    scanner = container.resolve(MarketScanner)
    top = await scanner.scan_all()
    return f"scan produced {len(top)} candidates"


async def analyze_cycle(ctx: dict[str, Any], symbols: list[str] | None = None) -> str:
    """Phase 3 analysis cycle: technical, news & fundamental for the candidates."""
    from qtrader.application.agents.fundamental import FundamentalAgent
    from qtrader.application.agents.news import NewsAgent
    from qtrader.application.agents.scanner import MarketScanner
    from qtrader.application.agents.technical import TechnicalAgent
    from qtrader.config.container import get_container

    container = get_container()
    scanner = container.resolve(MarketScanner)
    if symbols is None:
        top = await scanner.scan_all()
        symbols = [c.symbol for c in top]
    symbols = _owned(symbols)
    technical = await container.resolve(TechnicalAgent).analyze_candidates(symbols)
    news = await container.resolve(NewsAgent).analyze_candidates(symbols)
    fundamental = await container.resolve(FundamentalAgent).analyze_candidates(symbols)
    return (
        f"analyzed {len(symbols)} symbols: "
        f"technical={technical} news={news} fundamental={fundamental}"
    )


async def predict_cycle(ctx: dict[str, Any], symbols: list[str] | None = None) -> str:
    """Prediction cycle: probability-of-movement for the current candidates."""
    from qtrader.application.agents.prediction import PredictionAgent
    from qtrader.application.agents.scanner import MarketScanner
    from qtrader.config.container import get_container

    container = get_container()
    scanner = container.resolve(MarketScanner)
    if symbols is None:
        top = await scanner.scan_all()
        symbols = [c.symbol for c in top]
    symbols = _owned(symbols)
    predicted = await container.resolve(PredictionAgent).predict_candidates(symbols)
    return f"predicted {predicted}/{len(symbols)} symbols"


async def decide_cycle(ctx: dict[str, Any], symbols: list[str] | None = None) -> str:
    """Chief cycle: fused BUY/SELL/HOLD decisions for the current candidates."""
    from qtrader.application.agents.chief import ChiefAgent
    from qtrader.application.agents.scanner import MarketScanner
    from qtrader.config.container import get_container

    container = get_container()
    scanner = container.resolve(MarketScanner)
    if symbols is None:
        top = await scanner.scan_all()
        symbols = [c.symbol for c in top]
    symbols = _owned(symbols)
    decided = await container.resolve(ChiefAgent).decide_candidates(symbols)
    return f"decided {decided}/{len(symbols)} symbols"


async def risk_cycle(ctx: dict[str, Any]) -> str:
    """Phase 5 risk gate: re-assess the latest decision for each candidate."""
    from qtrader.application.agents.risk import RiskAgent
    from qtrader.application.agents.scanner import MarketScanner
    from qtrader.config.container import get_container
    from qtrader.domain.events import DecisionMade
    from qtrader.domain.ports import DecisionRepository
    from qtrader.domain.value_objects import Decision

    container = get_container()
    scanner = container.resolve(MarketScanner)
    decisions = container.resolve(DecisionRepository)
    top = await scanner.scan_all()
    approved = rejected = 0
    for symbol in _owned([c.symbol for c in top]):
        latest = await decisions.latest_for_symbol(symbol, limit=1)
        if not latest or latest[0].decision is Decision.HOLD:
            continue
        record = latest[0]
        assessment = await container.resolve(RiskAgent).assess_symbol(
            DecisionMade(
                decision_uuid=record.decision_uuid,
                symbol=symbol,
                decision=record.decision,
                confidence=float(record.confidence or 0),
                rationale=record.rationale or "",
                agent_scores=record.agent_scores,
            )
        )
        if assessment.approved:
            approved += 1
        else:
            rejected += 1
    return f"risk gate: approved={approved} rejected={rejected}"


async def execute_cycle(ctx: dict[str, Any]) -> str:
    """Phase 5 execution cycle: submit any pending (unsubmitted) orders."""
    from qtrader.application.agents.execution import ExecutionAgent
    from qtrader.application.services.portfolio_service import PortfolioService
    from qtrader.config.container import get_container
    from qtrader.domain.ports import OrderRepository
    from qtrader.domain.value_objects import OrderStatus

    container = get_container()
    portfolio = await container.resolve(PortfolioService).default_portfolio()
    pending = await container.resolve(OrderRepository).list_by_portfolio(
        portfolio.portfolio_id or 1, status=OrderStatus.PENDING.value, limit=50
    )
    execution = container.resolve(ExecutionAgent)
    executed = 0
    for order in pending:
        if await execution.execute_order(order) is not None:
            executed += 1
    return f"executed {executed}/{len(pending)} pending orders"


async def train_cycle(ctx: dict[str, Any], symbols: list[str] | None = None) -> str:
    """Nightly model training: fit + register + promote when the threshold passes."""
    from qtrader.application.services.model_trainer import ModelTrainer
    from qtrader.config.container import get_container

    container = get_container()
    settings = container.resolve(Settings)
    if symbols is None:
        symbols = settings.watchlist_symbols
    result = await container.resolve(ModelTrainer).train(
        symbols,
        settings.scan_interval,
        horizon_bars=settings.train_horizon_bars,
        lookback_bars=settings.train_lookback_bars,
        min_samples=settings.train_min_samples,
        promote_threshold=settings.train_promote_threshold,
    )
    if result is None:
        return "train: insufficient samples"
    acc = result.metrics.get("accuracy")
    return (
        f"train: {result.name} v{result.version} "
        f"acc={acc} promoted={result.promoted}"
    )


async def backtest_cycle(ctx: dict[str, Any]) -> str:
    """Phase 6: replay stored history, persist results, evaluate SystemGate."""
    from datetime import date
    from decimal import Decimal

    from qtrader.application.services.backtest import BacktestParams, BacktestRunner
    from qtrader.application.services.system_gate import SystemGate
    from qtrader.config.container import get_container
    from qtrader.domain.value_objects import Interval, TradingMode

    container = get_container()
    settings = container.resolve(Settings)
    if settings.backtest_universe:
        symbols = [s.strip().upper() for s in settings.backtest_universe.split(",") if s.strip()]
    else:
        symbols = settings.watchlist_symbols
    end = date.today()
    start = end - timedelta(days=settings.backtest_lookback_days)
    result = await container.resolve(BacktestRunner).run(
        name=f"auto-{end.isoformat()}",
        symbols=symbols,
        start=start,
        end=end,
        initial_capital=Decimal(str(settings.portfolio_initial_capital)),
        params=BacktestParams(
            interval=Interval(settings.backtest_interval),
            strategy=settings.gate_strategy,
            commission_bps=settings.backtest_commission_bps,
            slippage_bps=settings.backtest_slippage_bps,
            warmup_bars=settings.backtest_warmup_bars,
        ),
    )
    decision = await container.resolve(SystemGate).evaluate(
        settings.gate_strategy, TradingMode.PAPER
    )
    summary = result.summary
    return (
        f"backtest: {summary.trades_count} trades, "
        f"return={summary.total_return} sharpe={summary.sharpe} "
        f"gate={decision.status.value}"
    )


class WorkerSettings:
    functions = [
        heartbeat,
        backfill,
        scan_cycle,
        analyze_cycle,
        predict_cycle,
        decide_cycle,
        risk_cycle,
        execute_cycle,
        train_cycle,
        backtest_cycle,
    ]

    cron_jobs = [
        cron(heartbeat, name="heartbeat", second=0),
        cron(scan_cycle, name="scan_cycle", minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(analyze_cycle, name="analyze_cycle", minute={2, 17, 32, 47}),
        cron(predict_cycle, name="predict_cycle", minute={4, 19, 34, 49}),
        cron(decide_cycle, name="decide_cycle", minute={6, 21, 36, 51}),
        cron(risk_cycle, name="risk_cycle", minute={7, 22, 37, 52}),
        cron(execute_cycle, name="execute_cycle", minute={8, 23, 38, 53}),
        cron(train_cycle, name="train_cycle", hour={2}, minute=0),
        cron(backtest_cycle, name="backtest_cycle", hour={3}, minute=0),
    ]

    redis_settings = RedisSettings.from_dsn(Settings().redis_url)

    max_tries = 3
    job_timeout = 60
