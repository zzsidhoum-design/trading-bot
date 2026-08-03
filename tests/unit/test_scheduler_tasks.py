"""Unit tests for worker job-context logging hooks (arq)."""

from __future__ import annotations

import json
from decimal import Decimal

import structlog

from qtrader.config.logging import configure_logging, get_logger
from qtrader.config.settings import Settings
from qtrader.domain.entities import AgentMetric
from qtrader.domain.ports import AgentMetricRepository
from qtrader.domain.value_objects import TradingMode
from qtrader.infrastructure.schedulers.tasks import (
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
