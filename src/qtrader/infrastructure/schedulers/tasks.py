"""Scheduled worker tasks (arq).

Each task receives ``ctx`` with ``redis`` (arq's connection) and any kwargs
from the cron entry. Tasks must be idempotent — arq guarantees at-least-once.
The DI container is created once in ``on_startup``, stored in ``ctx`` and
closed in ``on_shutdown`` so every task shares a single engine/redis/event bus.

The trading pipeline is event-driven: ``scan_cycle`` publishes
``ScanCompleted`` and the subscribed agents cascade through analysis,
prediction, decisions, risk, allocation and execution on the in-process
event bus. ``execute_cycle`` is a safety net that submits any PENDING
orders left behind (e.g. created via the API or after a crash).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from qtrader.config.settings import Settings
from qtrader.domain.value_objects import Interval

CONTAINER_KEY = "container"


def _container(ctx: dict[str, Any]) -> Any:
    """The shared worker container, created in ``on_startup``."""
    from qtrader.config.container import Container

    container = ctx.get(CONTAINER_KEY)
    if not isinstance(container, Container):
        raise RuntimeError(
            f"worker {CONTAINER_KEY!r} not initialized (missing on_startup hook?)"
        )
    return container


async def _startup(ctx: dict[str, Any]) -> None:
    """Create the process-wide container once and share it via ctx."""
    from qtrader.config.container import get_container

    ctx[CONTAINER_KEY] = get_container()
    await _ensure_watchlist_active(ctx[CONTAINER_KEY])


async def _ensure_watchlist_active(container: Any) -> None:
    """Register every watchlist symbol in the DB as active.

    Without this the scanner would find no tradable symbols: the scanner only
    iterates ``list_active()``, and watchlist symbols created by tests or older
    seeds can be left inactive. Runs at worker startup so the pipeline is
    self-healing after a fresh database.
    """
    from qtrader.domain.entities import Stock
    from qtrader.domain.ports import StockRepository

    settings = container.resolve(Settings)
    stocks = container.resolve(StockRepository)
    for symbol in _owned(settings.watchlist_symbols):
        existing = await stocks.get_by_symbol(symbol)
        if existing is not None:
            if existing.is_active:
                continue
            await stocks.upsert(
                Stock(
                    symbol=symbol,
                    exchange=existing.exchange,
                    name=existing.name or symbol,
                    is_active=True,
                )
            )
            continue
        await stocks.upsert(Stock(symbol=symbol, exchange="XNAS", name=symbol, is_active=True))


async def _shutdown(ctx: dict[str, Any]) -> None:
    """Close the shared container so the worker exits cleanly."""
    container = ctx.get(CONTAINER_KEY)
    if container is not None:
        await container.aclose()
        ctx[CONTAINER_KEY] = None


async def _on_job_start(ctx: dict[str, Any]) -> None:
    """Bind per-job context for structured logs; drop any stale context.

    arq reuses one process for every job, so without clearing, contextvars
    (correlation IDs etc.) from the previous job would leak into the next.
    """
    import structlog

    from qtrader.config.logging import set_correlation_id

    structlog.contextvars.clear_contextvars()
    job_id = str(ctx.get("job_id", ""))
    structlog.contextvars.bind_contextvars(job=ctx.get("job_name", ""), job_id=job_id[:8])
    set_correlation_id(f"job:{job_id[:8]}")


async def _on_job_end(ctx: dict[str, Any]) -> None:
    """Release per-job logging context."""
    import structlog

    structlog.contextvars.clear_contextvars()


def _owned(symbols: list[str]) -> list[str]:
    """Filter ``symbols`` down to this worker's shard (see application/services/shard.py)."""
    from qtrader.application.services.shard import owned_symbols

    settings = Settings()
    return owned_symbols(symbols, settings.worker_shard_id, settings.worker_shards)


async def _record_agent_metric(
    container: Any,
    *,
    agent_name: str,
    metric_name: str,
    value: Decimal,
    window: str = "latest",
) -> None:
    """Best-effort dashboard metric write; never fails the job."""
    from qtrader.config.logging import get_logger
    from qtrader.domain.entities import AgentMetric
    from qtrader.domain.ports import AgentMetricRepository

    try:
        repo = container.resolve(AgentMetricRepository)
        await repo.record(
            AgentMetric(
                agent_name=agent_name,
                metric_name=metric_name,
                value=value,
                window=window,
            )
        )
    except Exception:
        get_logger("qtrader.worker").warning(
            "agent_metric.record_failed", agent=agent_name, metric=metric_name
        )


async def heartbeat(ctx: dict[str, Any]) -> str:
    """Prove the worker is alive: round-trip through the shared cache/DB."""
    from redis.asyncio import Redis

    from qtrader.infrastructure.cache import RedisCache

    container = _container(ctx)
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
    """Data Agent job: pull clean history for the watchlist (or one symbol).

    Fetches the configured intraday interval plus daily bars — the risk gate
    and backtester size positions from daily ATR, so daily data must exist
    for the pipeline to proceed past Risk.
    """
    from qtrader.application.agents.data import DataAgent
    from qtrader.application.services.indicators import IndicatorEngine
    from qtrader.domain.ports import IndicatorRepository, PriceRepository

    container = _container(ctx)
    settings = container.resolve(Settings)
    agent = container.resolve(DataAgent)
    engine = IndicatorEngine()
    indicators = container.resolve(IndicatorRepository)
    prices = container.resolve(PriceRepository)
    iv = Interval(interval) if interval else settings.scan_interval
    symbols = [symbol] if symbol else settings.watchlist_symbols
    symbols = _owned(symbols)
    end = datetime.now(UTC)
    start = end - timedelta(days=days or settings.backfill_days)
    total = 0
    for sym in symbols:
        total += await agent.backfill(sym, iv, start, end)
        if iv is not Interval.D1:
            total += await agent.backfill(sym, Interval.D1, start, end)
        for snap_iv in {iv, Interval.D1}:
            bars = await prices.history(sym, snap_iv, limit=400)
            if len(bars) >= 15:
                snapshot = engine.compute(bars, sym, snap_iv)
                await indicators.save_snapshot(snapshot)
    return f"backfilled {total} bars for {len(symbols)} symbols ({iv}+D1)"


async def scan_cycle(ctx: dict[str, Any]) -> str:
    """Market Scanner pulse: recompute top-K rankings.

    Publishing ``ScanCompleted`` cascades the whole pipeline through the
    event bus (analysis → prediction → decisions → risk → execution).
    """
    from qtrader.application.agents.scanner import MarketScanner

    container = _container(ctx)
    scanner = container.resolve(MarketScanner)
    top = await scanner.scan_all()
    await _record_agent_metric(
        container,
        agent_name="scanner",
        metric_name="candidates",
        value=Decimal(len(top)),
    )
    return f"scan produced {len(top)} candidates"


async def execute_cycle(ctx: dict[str, Any]) -> str:
    """Phase 5 execution cycle: submit any pending (unsubmitted) orders."""
    from qtrader.application.agents.execution import ExecutionAgent
    from qtrader.application.services.portfolio_service import PortfolioService
    from qtrader.domain.ports import OrderRepository
    from qtrader.domain.value_objects import OrderStatus

    container = _container(ctx)
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

    container = _container(ctx)
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
    await _record_agent_metric(
        container,
        agent_name="trainer",
        metric_name="accuracy",
        value=Decimal(str(acc)) if acc is not None else Decimal(0),
    )
    await _record_agent_metric(
        container,
        agent_name="trainer",
        metric_name="promoted",
        value=Decimal(1) if result.promoted else Decimal(0),
    )
    return (
        f"train: {result.name} v{result.version} "
        f"acc={acc} promoted={result.promoted}"
    )


async def backtest_cycle(ctx: dict[str, Any]) -> str:
    """Phase 6: replay stored history, persist results, evaluate SystemGate."""
    from datetime import date

    from qtrader.application.services.backtest import BacktestParams, BacktestRunner
    from qtrader.application.services.system_gate import SystemGate
    from qtrader.domain.value_objects import Interval, TradingMode

    container = _container(ctx)
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
    await _record_agent_metric(
        container,
        agent_name="backtester",
        metric_name="total_return",
        value=summary.total_return or Decimal(0),
    )
    return (
        f"backtest: {summary.trades_count} trades, "
        f"return={summary.total_return} sharpe={summary.sharpe} "
        f"gate={decision.status.value}"
    )


async def walk_forward_cycle(ctx: dict[str, Any]) -> str:
    """Phase 6: refit on past folds only, validate OOS, update the gate."""
    from datetime import date

    from qtrader.application.services.walk_forward import STRATEGY_LABEL, WalkForwardValidator
    from qtrader.domain.value_objects import Interval

    container = _container(ctx)
    settings = container.resolve(Settings)
    if settings.backtest_universe:
        symbols = [s.strip().upper() for s in settings.backtest_universe.split(",") if s.strip()]
    else:
        symbols = settings.watchlist_symbols
    end = date.today()
    start = end - timedelta(days=settings.backtest_lookback_days)
    summary = await container.resolve(WalkForwardValidator).validate(
        symbols=symbols,
        start=start,
        end=end,
        initial_capital=Decimal(str(settings.portfolio_initial_capital)),
        interval=Interval(settings.backtest_interval),
        commission_bps=settings.backtest_commission_bps,
        slippage_bps=settings.backtest_slippage_bps,
    )
    if summary is None:
        return "walk-forward: skipped (insufficient history)"
    await _record_agent_metric(
        container,
        agent_name="walk_forward",
        metric_name="total_return",
        value=summary.total_return or Decimal(0),
    )
    return (
        f"walk-forward: {summary.trades_count} trades, "
        f"return={summary.total_return} sharpe={summary.sharpe} "
        f"oos={STRATEGY_LABEL}"
    )


class WorkerSettings:
    functions = [
        heartbeat,
        backfill,
        scan_cycle,
        execute_cycle,
        train_cycle,
        backtest_cycle,
        walk_forward_cycle,
    ]

    cron_jobs = [
        cron(heartbeat, name="heartbeat", second=0),
        cron(scan_cycle, name="scan_cycle", minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(execute_cycle, name="execute_cycle", minute={8, 23, 38, 53}),
        cron(train_cycle, name="train_cycle", hour={2}, minute=0),
        cron(backtest_cycle, name="backtest_cycle", hour={3}, minute=0),
        cron(walk_forward_cycle, name="walk_forward_cycle", hour={3}, minute=5),
    ]

    redis_settings = RedisSettings.from_dsn(Settings().redis_url)

    on_startup = _startup
    on_shutdown = _shutdown
    on_job_start = _on_job_start
    on_job_end = _on_job_end

    max_tries = 3
    job_timeout = 60
