"""Centralized structured logging configuration.

Configures structlog with JSON output, correlation IDs, and standard fields.
Integrates with stdlib logging for third-party libraries.
"""

from __future__ import annotations

import logging
import time as _time
import uuid
from contextvars import ContextVar
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, cast

import structlog
from structlog.types import EventDict, Processor

from qtrader.config.settings import Settings
from qtrader.domain.value_objects import Money

_correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def _add_correlation_id(
    logger: Any, method_name: str, event_dict: EventDict
) -> EventDict:
    """Add correlation ID from contextvars if available."""
    cid = _correlation_id_var.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def _add_service_context(
    logger: Any, method_name: str, event_dict: EventDict
) -> EventDict:
    """Add service-level context."""
    event_dict["service"] = "qtrader"
    return event_dict


def _ensure_timestamp(
    logger: Any, method_name: str, event_dict: EventDict
) -> EventDict:
    """Ensure timestamp is present and ISO formatted."""
    if "timestamp" not in event_dict:
        from datetime import UTC, datetime

        event_dict["timestamp"] = datetime.now(UTC).isoformat()
    return event_dict


def _drop_color_message_key(
    logger: Any, method_name: str, event_dict: EventDict
) -> EventDict:
    """Remove color_message key added by structlog.dev.ConsoleRenderer."""
    event_dict.pop("color_message", None)
    return event_dict


def _rename_event_key(
    logger: Any, method_name: str, event_dict: EventDict
) -> EventDict:
    """Rename 'event' to 'message' for consistency."""
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


def _add_log_level(
    logger: Any, method_name: str, event_dict: EventDict
) -> EventDict:
    """Ensure level is present."""
    if "level" not in event_dict:
        event_dict["level"] = "info"
    return event_dict


def _json_default(value: Any) -> Any:
    """JSON fallback for non-serializable log values (Decimal, Money, dates).

    A crashing logger would otherwise take down the request/handler that
    triggered it, so every unknown value degrades to a string instead.
    """
    if isinstance(value, Money):
        return str(value.amount)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return [str(v) for v in value]
    return str(value)


def configure_logging(settings: Settings | None = None) -> None:
    """Configure structlog and stdlib logging.

    Call once at application startup (in lifespan or container init).

    Args:
        settings: Optional Settings instance. If None, uses defaults.
    """
    settings = settings or Settings()

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Configure stdlib logging first (for third-party libs)
    # stream=None → StreamHandler resolves sys.stdout lazily at emit time,
    # so pytest capsys / tests capturing stdout work correctly.
    logging.basicConfig(
        format="%(message)s",
        stream=None,
        level=log_level,
        force=True,
    )

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("redis").setLevel(logging.WARNING)
    logging.getLogger("arq").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Processors for all environments
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_service_context,
        _add_correlation_id,
        _add_log_level,
        _ensure_timestamp,
        _rename_event_key,
        _drop_color_message_key,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.qtrader_mode.value == "backtest" and not settings.enable_live_trading:
        # Development: pretty console output
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True)
        ]
    else:
        # Production: JSON output (never crashes on Decimal/Money/dates).
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(default=_json_default),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a configured structlog logger.

    Usage:
        logger = get_logger(__name__)
        logger.info("order_submitted", symbol="AAPL", qty=10, price=150.0)
    """
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


class LoggingMiddleware:
    """ASGI middleware: per-request correlation ID + structured request log.

    Adds a short correlation ID to the structlog context for the request and
    logs one ``http.request`` line per completed request (method, path,
    status, duration) — the structured replacement for an access log.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = str(uuid.uuid4())[:8]
        token = _correlation_id_var.set(correlation_id)
        start = _time.perf_counter()
        status_code = 500

        async def _send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            duration_ms = (_time.perf_counter() - start) * 1000
            get_logger("qtrader.http").info(
                "http.request",
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                status=status_code,
                duration_ms=round(duration_ms, 2),
                correlation_id=correlation_id,
            )
            _correlation_id_var.reset(token)


def set_correlation_id(correlation_id: str) -> None:
    """Set correlation ID for current context (e.g., from message headers)."""
    _correlation_id_var.set(correlation_id)


def bind_context(**kwargs: Any) -> None:
    """Bind key-value pairs to current logging context."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    """Clear all context variables."""
    structlog.contextvars.clear_contextvars()
