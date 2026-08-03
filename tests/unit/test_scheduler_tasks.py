"""Unit tests for worker job-context logging hooks (arq)."""

from __future__ import annotations

import json

import structlog

from qtrader.config.logging import configure_logging, get_logger
from qtrader.config.settings import Settings
from qtrader.domain.value_objects import TradingMode
from qtrader.infrastructure.schedulers.tasks import _on_job_end, _on_job_start

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
