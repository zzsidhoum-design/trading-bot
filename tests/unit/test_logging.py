"""Unit tests for centralized structured logging configuration."""

from __future__ import annotations

import json

import structlog

from qtrader.config.logging import (
    clear_context,
    configure_logging,
    get_logger,
    set_correlation_id,
)
from qtrader.config.settings import Settings
from qtrader.domain.value_objects import TradingMode


def _configure_json() -> None:
    """Configure logging in production (JSON) mode for tests."""
    settings = Settings(
        _env_file=None,
        qtrader_mode=TradingMode.PAPER,
        enable_live_trading=False,
    )
    configure_logging(settings)


def _captured(capsys) -> dict:
    """Parse the most recent JSON log line from captured stderr."""
    captured = capsys.readouterr()
    line = (captured.err or captured.out).strip().splitlines()[-1]
    return json.loads(line)


def test_configure_logging_produces_json_in_production_mode(capsys) -> None:
    """In non-backtest mode, logs render as JSON lines."""
    _configure_json()

    logger = get_logger("test.json.logger")
    logger.info("test.event", symbol="AAPL", value=42)
    parsed = _captured(capsys)

    assert parsed["message"] == "test.event"
    assert parsed["symbol"] == "AAPL"
    assert parsed["value"] == 42
    assert parsed["service"] == "qtrader"
    assert "timestamp" in parsed
    assert parsed["level"] == "info"


def test_correlation_id_is_included(capsys) -> None:
    """Correlation IDs bound to context appear in log output."""
    _configure_json()
    set_correlation_id("abc123")

    logger = get_logger("test.correlation.logger")
    logger.info("test.correlation", symbol="AAPL")
    parsed = _captured(capsys)

    assert parsed["correlation_id"] == "abc123"
    clear_context()


def test_get_logger_returns_bound_logger() -> None:
    """get_logger returns a usable bound logger."""
    logger = get_logger("test.plain.logger")
    assert logger is not None
    assert hasattr(logger, "info")


def test_logging_can_handle_none_values(capsys) -> None:
    """Loggers handle None values gracefully (no crash)."""
    _configure_json()
    logger = get_logger("test.none.logger")
    logger.info("test.none", value=None)
    parsed = _captured(capsys)
    assert parsed["message"] == "test.none"
    assert parsed["value"] is None


def test_structlog_module_level_works(capsys) -> None:
    """Module-level structlog loggers work after configure_logging."""
    _configure_json()
    logger = structlog.get_logger("test.module.level")
    logger.info("module.event", foo="bar")
    parsed = _captured(capsys)
    assert parsed["message"] == "module.event"
    assert parsed["foo"] == "bar"


async def test_http_middleware_logs_request_with_correlation(capsys) -> None:
    """LoggingMiddleware emits a structured http.request line per call."""
    from qtrader.config.logging import LoggingMiddleware

    _configure_json()

    async def downstream(scope, receive, send) -> None:
        return None

    app = LoggingMiddleware(downstream)
    scope = {"type": "http", "method": "GET", "path": "/api/v1/health"}
    response_started = False

    async def receive() -> None:
        return None

    async def send(message) -> None:
        nonlocal response_started
        if message["type"] == "http.response.start":
            response_started = True

    await app(scope, receive, send)
    parsed = _captured(capsys)
    assert parsed["message"] == "http.request"
    assert parsed["method"] == "GET"
    assert parsed["path"] == "/api/v1/health"
    assert parsed["status"] == 500
    assert "duration_ms" in parsed
    assert len(parsed["correlation_id"]) == 8
