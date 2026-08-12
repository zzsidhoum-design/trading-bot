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


async def _market_open(container: Any) -> bool:
    """False while the exchange is closed; log the next open so operators can see why jobs idle.

    Trading-cycle jobs (backfill, scan, execute) only make sense during a live
    session: there is no new data, no new candidates and no orders to route while
    the market is closed. Maintenance jobs (train/backtest/walk-forward) and the
    heartbeat are deliberately not gated.
    """
    from qtrader.config.logging import get_logger

    settings = container.resolve(Settings)
    hours = settings.market_hours
    now = datetime.now(UTC)
    if hours.is_open(now):
        return True
    get_logger("qtrader.worker").info(
        "market.closed",
        next_open=hours.next_open(now).isoformat(),
        session=(
            f"{hours.open_time.strftime('%H:%M')}-{hours.close_time.strftime('%H:%M')} "
            f"{hours.timezone_name}"
        ),
    )
    return False


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


async def backfill_cycle(ctx: dict[str, Any]) -> str:
    """Market-hours data refresh: keep the intraday window warm for the scanner.

    Runs every 15 minutes while the exchange is open. The short lookback is
    intentional — intraday bars only exist for recent sessions, and a small
    payload keeps the provider request cheap on each tick.
    """
    container = _container(ctx)
    if not await _market_open(container):
        return "market closed — backfill skipped"
    return await backfill(ctx, days=container.resolve(Settings).backfill_intraday_days)


async def data_quality_cycle(ctx: dict[str, Any]) -> str:
    """Periodic data-quality audit over the persisted price universe.

    Never gated by market hours: the audit verifies structural integrity,
    coverage and freshness of whatever the pipeline has persisted. Failing
    checks are logged as warnings and the overall score is recorded as an
    agent metric so the dashboard can trend it.
    """
    from qtrader.application.services.data_quality import DataQualityAuditor
    from qtrader.config.logging import get_logger

    container = _container(ctx)
    settings = container.resolve(Settings)
    auditor = container.resolve(DataQualityAuditor)
    report = await auditor.audit(_owned(settings.watchlist_symbols))
    logger = get_logger("qtrader.worker")
    for check in report.checks:
        record = logger.info if check.passed else logger.warning
        record(
            "data_quality.check",
            check=check.name,
            status=check.status,
            detail=check.detail,
        )
    await _record_agent_metric(
        container,
        agent_name="data_quality",
        metric_name="score",
        value=Decimal(f"{report.score:.3f}"),
    )
    failed = [c.name for c in report.checks if not c.passed]
    return (
        f"data-quality {report.verdict} score={report.score:.2f} "
        f"failed={failed or 'none'}"
    )


async def universe_cycle(ctx: dict[str, Any]) -> str:
    """Phase 2: refresh the dynamic trading universe.

    Runs discovery, applies the configurable liquidity/tier filters, persists
    membership lifecycle (new listings / suspensions / delistings / renames)
    and reports coverage. Never gated by market hours — discovery runs
    off-hours so the next trading day sees an up-to-date universe. Newly added
    members are daily-backfilled so their metrics stabilise.
    """
    from qtrader.application.services.universe import UniverseEngine

    container = _container(ctx)
    settings = container.resolve(Settings)
    engine = container.resolve(UniverseEngine)
    report = await engine.refresh()
    for symbol in report.added:
        await backfill(ctx, symbol=symbol, interval="1d", days=settings.backfill_days)
    snapshot = await engine.snapshot()
    await _record_agent_metric(
        container,
        agent_name="universe",
        metric_name="tradable",
        value=Decimal(snapshot["tradable"]),
    )
    return (
        f"universe source={report.source} discovered={report.discovered} "
        f"added={len(report.added)} suspended={len(report.suspended)} "
        f"delisted={len(report.delisted)} resumed={len(report.resumed)} "
        f"renames={len(report.symbol_changes)} tradable={snapshot['tradable']}"
    )


async def scan_cycle(ctx: dict[str, Any]) -> str:
    """Market Scanner pulse: recompute top-K rankings.

    Publishing ``ScanCompleted`` cascades the whole pipeline through the
    event bus (analysis → prediction → decisions → risk → execution).
    """
    from qtrader.application.agents.scanner import MarketScanner

    container = _container(ctx)
    if not await _market_open(container):
        return "market closed — scan skipped"
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
    if not await _market_open(container):
        return "market closed — execution skipped"
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


async def research_cycle(ctx: dict[str, Any]) -> str:
    """Phase 3: multi-timeframe research — which timeframes/combos are useful.

    Explicitly research-only (no strategies are built or traded). Runs the pure
    study engine over the resolved universe and logs the ranked recommendations,
    best roles and any data limitations. Never gated by market hours: research
    consumes whatever history is persisted and is meant to run off-hours.
    """
    from qtrader.application.services.multitimeframe import (
        MultitimeframeResearchEngine,
    )
    from qtrader.config.logging import get_logger

    container = _container(ctx)
    settings = container.resolve(Settings)
    logger = get_logger("qtrader.worker")
    report = await container.resolve(MultitimeframeResearchEngine).run()
    logger.info(
        "research.report",
        symbols=len(report.symbols),
        timeframe_studies=len(report.timeframe_studies),
        combinations=len(report.combinations),
        recommendations=len(report.recommendations),
        best_context=report.best_context.value,
        best_setup=report.best_setup.value,
        best_entry=report.best_entry.value,
        lookback_days=settings.research_lookback_days,
        limitations=report.limitations,
    )
    top = report.recommendations[:3]
    top_label = ", ".join(f"{r.combo.key}@o={r.oos_sharpe:.2f}" for r in top) or "none"
    return (
        f"research: {len(report.combinations)} combos, "
        f"best={report.best_context.value}/{report.best_setup.value}/"
        f"{report.best_entry.value}, top=[{top_label}]"
    )


class WorkerSettings:
    functions = [
        heartbeat,
        backfill,
        backfill_cycle,
        data_quality_cycle,
        universe_cycle,
        scan_cycle,
        execute_cycle,
        train_cycle,
        backtest_cycle,
        walk_forward_cycle,
        research_cycle,
    ]

    cron_jobs = [
        cron(heartbeat, name="heartbeat", second=0),
        cron(backfill_cycle, name="backfill_cycle", minute={0, 15, 30, 45}),
        cron(data_quality_cycle, name="data_quality_cycle", minute={0, 30}),
        cron(
            universe_cycle,
            name="universe_cycle",
            hour={Settings().universe_refresh_hour},
            minute=0,
        ),
        cron(scan_cycle, name="scan_cycle", minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(execute_cycle, name="execute_cycle", minute={8, 23, 38, 53}),
        cron(train_cycle, name="train_cycle", hour={2}, minute=0),
        cron(backtest_cycle, name="backtest_cycle", hour={3}, minute=0),
        cron(walk_forward_cycle, name="walk_forward_cycle", hour={3}, minute=5),
        cron(research_cycle, name="research_cycle", hour={3}, minute=15),
    ]

    redis_settings = RedisSettings.from_dsn(Settings().redis_url)

    on_startup = _startup
    on_shutdown = _shutdown
    on_job_start = _on_job_start
    on_job_end = _on_job_end

    max_tries = 3
    job_timeout = 60
