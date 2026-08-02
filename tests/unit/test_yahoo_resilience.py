"""Fault-injection tests for the Yahoo provider (Phase 8 hardening).

Uses httpx MockTransport so no network is touched: we simulate transient
5xx/connection failures, circuit opening, and recovery.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import pytest

from qtrader.domain.value_objects import Interval
from qtrader.infrastructure.data_providers.yahoo import YahooFinanceProvider
from qtrader.infrastructure.resilience import BreakerState

_START = datetime(2026, 1, 1, tzinfo=UTC)
_END = datetime(2026, 1, 3, tzinfo=UTC)

_PAYLOAD: dict = {
    "chart": {
        "result": [
            {
                "timestamp": [1767225600, 1767229200],
                "indicators": {
                    "quote": [
                        {
                            "open": [100.0, 101.0],
                            "high": [102.0, 103.0],
                            "low": [99.0, 100.0],
                            "close": [101.5, 102.5],
                            "volume": [1000, 1100],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}


def _provider_with(handler: httpx.MockTransport, **kwargs: object) -> YahooFinanceProvider:
    client = httpx.AsyncClient(transport=handler, base_url="https://example.test")
    return YahooFinanceProvider(client=client, **kwargs)


def test_retries_transient_errors_then_succeeds() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests < 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=_PAYLOAD, request=request)

    provider = _provider_with(httpx.MockTransport(handler))
    try:
        bars = asyncio_run(provider.fetch_bars("AAPL", Interval.D1, _START, _END))
    finally:
        asyncio_run(provider.close())
    assert requests == 3
    assert len(bars) == 2


def test_no_retry_on_client_error() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(404, request=request)

    provider = _provider_with(httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="failed"):
            asyncio_run(provider.fetch_bars("AAPL", Interval.D1, _START, _END))
    finally:
        asyncio_run(provider.close())
    assert requests == 1


def test_circuit_opens_after_consecutive_failures() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500, request=request)

    provider = _provider_with(
        httpx.MockTransport(handler),
        circuit=make_breaker(failure_threshold=2),
    )
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio_run(provider.fetch_bars("AAPL", Interval.D1, _START, _END))
        assert provider._circuit.state is BreakerState.OPEN  # type: ignore[attr-defined]
        # subsequent calls fail fast without hitting the transport
        before = requests
        with pytest.raises(RuntimeError, match="circuit open"):
            asyncio_run(provider.fetch_quote("AAPL"))
        assert requests == before
    finally:
        asyncio_run(provider.close())


def test_circuit_recovers_after_reset_timeout() -> None:
    from qtrader.infrastructure.resilience import CircuitBreaker

    breaker = CircuitBreaker(name="yahoo", failure_threshold=1, reset_timeout_seconds=0.05)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:  # the first call's 3 retries all fail → breaker opens
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=_PAYLOAD, request=request)

    provider = _provider_with(httpx.MockTransport(handler), circuit=breaker)
    try:
        with pytest.raises(RuntimeError):
            asyncio_run(provider.fetch_bars("AAPL", Interval.D1, _START, _END))
        assert breaker.state is BreakerState.OPEN
        time.sleep(0.06)
        bars = asyncio_run(provider.fetch_bars("AAPL", Interval.D1, _START, _END))
        assert len(bars) == 2
        assert breaker.state is BreakerState.CLOSED
    finally:
        asyncio_run(provider.close())


def make_breaker(*, failure_threshold: int):
    from qtrader.infrastructure.resilience import CircuitBreaker

    return CircuitBreaker(name="yahoo", failure_threshold=failure_threshold)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
