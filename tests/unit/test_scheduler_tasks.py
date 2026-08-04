"""Unit tests for worker job-context logging hooks (arq)."""

from __future__ import annotations

import json
from decimal import Decimal

import structlog

from qtrader.config.logging import configure_logging, get_logger
from qtrader.config.settings import Settings
from qtrader.domain.entities import AgentMetric, Stock
from qtrader.domain.ports import AgentMetricRepository
from qtrader.domain.value_objects import TradingMode
from qtrader.infrastructure.schedulers.tasks import (
    _ensure_watchlist_active,
    _on_job_end,
    _on_job_start,
    _record_agent_metric,
)

JOB_ID = "12345678-1234-1234-1234-123456789abc"


async def test_on_job_start_binds_job_context(capsys) -> None:
    """Job name/id reach rendered log lines as structured fields."""
    configure_logging(
        Settings(_env_file=None, qtrader_mode=TradingMode.PAPER, enable_live_trading=False)
    )
    ctx = {"job_name": "scan_cycle", "job_id": JOB_ID}
    await _on_job_start(ctx)
    try:
        merged = structlog.contextvars.get_contextvars()
        assert merged["job"] == "scan_cycle"
        assert merged["job_id"] == "12345678"

        get_logger("test.worker.job").info("job.event", symbol="AAPL")
        captured = capsys.readouterr()
        parsed = json.loads((captured.err or captured.out).strip().splitlines()[-1])
        assert parsed["message"] == "job.event"
        assert parsed["job"] == "scan_cycle"
        assert parsed["job_id"] == "12345678"
        assert parsed["correlation_id"] == "job:12345678"
    finally:
        await _on_job_end(ctx)


async def test_on_job_end_clears_context() -> None:
    ctx = {"job_name": "scan_cycle", "job_id": JOB_ID}
    await _on_job_start(ctx)
    await _on_job_end(ctx)

    assert structlog.contextvars.get_contextvars() == {}


class _RecordingRepo:
    def __init__(self) -> None:
        self.recorded: list[AgentMetric] = []

    async def record(self, metric: AgentMetric) -> AgentMetric:
        self.recorded.append(metric)
        return metric


class _StubContainer:
    def __init__(self, repo: object) -> None:
        self._repo = repo

    def resolve(self, service_type: type) -> object:
        if service_type is AgentMetricRepository:
            return self._repo
        raise KeyError(service_type)


class _BrokenRepo:
    async def record(self, metric: AgentMetric) -> AgentMetric:
        raise RuntimeError("db down")


async def test_record_agent_metric_persists_via_container() -> None:
    repo = _RecordingRepo()
    container = _StubContainer(repo)

    await _record_agent_metric(
        container,
        agent_name="scanner",
        metric_name="candidates",
        value=Decimal(4),
    )

    assert len(repo.recorded) == 1
    assert repo.recorded[0].agent_name == "scanner"
    assert repo.recorded[0].metric_name == "candidates"
    assert repo.recorded[0].value == Decimal(4)
    assert repo.recorded[0].window == "latest"


async def test_record_agent_metric_never_raises() -> None:
    await _record_agent_metric(
        _StubContainer(_BrokenRepo()),
        agent_name="trainer",
        metric_name="accuracy",
        value=Decimal("0.5"),
    )


async def test_record_agent_metric_missing_repo_is_silent() -> None:
    await _record_agent_metric(
        _StubContainer(object()),
        agent_name="backtester",
        metric_name="total_return",
        value=Decimal("0.05"),
    )


class _RecordingStockRepo:
    def __init__(self, existing: dict[str, bool] | None = None) -> None:
        self.upserts: list[tuple[str, str, bool]] = []
        self._existing = existing or {}

    async def get_by_symbol(self, symbol: str, exchange: str | None = None):
        if symbol in self._existing:
            return Stock(
                symbol=symbol,
                exchange="XNAS" if self._existing[symbol] else "YAHOO",
                is_active=self._existing[symbol],
            )
        return None

    async def upsert(self, stock) -> Stock:
        self.upserts.append((stock.symbol, stock.exchange, stock.is_active))
        return stock


class _SettingsContainer:
    def __init__(self, symbols: list[str]) -> None:
        self._watchlist = ",".join(symbols)
        self._stocks = _RecordingStockRepo()

    def resolve(self, service_type: type) -> object:
        from qtrader.domain.ports import StockRepository

        if service_type is StockRepository:
            return self._stocks
        if service_type is Settings:
            return Settings(
                _env_file=None,
                watchlist=self._watchlist,
                worker_shards=1,
                worker_shard_id=0,
            )
        raise KeyError(service_type)

    @property
    def stocks(self) -> _RecordingStockRepo:
        return self._stocks


async def test_ensure_watchlist_active_upserts_watchlist_as_active() -> None:
    container = _SettingsContainer(["AAPL", "MSFT"])
    await _ensure_watchlist_active(container)
    assert container.stocks.upserts == [("AAPL", "XNAS", True), ("MSFT", "XNAS", True)]


async def test_ensure_watchlist_active_reuses_existing_inactive_row() -> None:
    container = _SettingsContainer(["AAPL", "MSFT"])
    container.stocks._existing = {"AAPL": False, "MSFT": True}
    await _ensure_watchlist_active(container)
    assert container.stocks.upserts == [("AAPL", "YAHOO", True)]
