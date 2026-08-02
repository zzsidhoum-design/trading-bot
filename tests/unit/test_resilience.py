"""Unit tests for the resilience primitives (Phase 8 hardening)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from qtrader.infrastructure.resilience import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitOpenError,
    TokenBucket,
    is_transient,
    retry_async,
)


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status_code, request=request)
    )


def test_is_transient_accepts_network_errors() -> None:
    assert is_transient(httpx.ConnectTimeout("timeout"))
    assert is_transient(httpx.ReadTimeout("timeout"))
    assert is_transient(httpx.ConnectError("refused"))
    assert is_transient(TimeoutError())


def test_is_transient_accepts_5xx_and_429() -> None:
    assert is_transient(_status_error(500))
    assert is_transient(_status_error(503))
    assert is_transient(_status_error(429))


def test_is_transient_rejects_4xx() -> None:
    assert not is_transient(_status_error(400))
    assert not is_transient(_status_error(404))


class TestCircuitBreaker:
    async def test_passes_through_when_closed(self) -> None:
        breaker = CircuitBreaker(name="t", failure_threshold=3)

        async def ok() -> str:
            return "ok"

        result = await breaker.call(ok)
        assert result == "ok"
        assert breaker.state is BreakerState.CLOSED
        assert breaker.consecutive_failures == 0

    async def test_opens_after_threshold(self) -> None:
        breaker = CircuitBreaker(name="t", failure_threshold=2, reset_timeout_seconds=60)

        async def boom() -> None:
            raise RuntimeError("down")

        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        assert breaker.state is BreakerState.OPEN
        assert breaker.consecutive_failures == 2

    async def test_fails_fast_while_open(self) -> None:
        breaker = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=60)

        async def boom() -> None:
            raise RuntimeError("down")

        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        assert breaker.state is BreakerState.OPEN
        with pytest.raises(CircuitOpenError):
            await breaker.call(boom)
        # the underlying function is never invoked while open
        calls = 0

        async def guarded() -> None:
            nonlocal calls
            calls += 1

        with pytest.raises(CircuitOpenError):
            await breaker.call(guarded)
        assert calls == 0

    async def test_recovers_after_reset_timeout(self) -> None:
        breaker = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=0.05)

        async def boom() -> None:
            raise RuntimeError("down")

        async def ok() -> str:
            return "recovered"

        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        assert breaker.state is BreakerState.OPEN
        await asyncio.sleep(0.06)
        assert await breaker.call(ok) == "recovered"
        assert breaker.state is BreakerState.CLOSED

    async def test_half_open_probe_failure_reopens(self) -> None:
        breaker = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=0.05)

        async def boom() -> None:
            raise RuntimeError("down")

        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        await asyncio.sleep(0.06)
        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        assert breaker.state is BreakerState.OPEN

    async def test_success_resets_failure_count(self) -> None:
        breaker = CircuitBreaker(name="t", failure_threshold=3)

        async def boom() -> None:
            raise RuntimeError("down")

        async def ok() -> None:
            return None

        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        await breaker.call(ok)
        assert breaker.consecutive_failures == 0
        assert breaker.state is BreakerState.CLOSED

    async def test_on_state_change_callback(self) -> None:
        transitions: list[tuple[str, str]] = []
        breaker = CircuitBreaker(
            name="t",
            failure_threshold=1,
            reset_timeout_seconds=0.05,
            on_state_change=lambda src, dst: transitions.append((src, dst)),
        )

        async def boom() -> None:
            raise RuntimeError("down")

        async def ok() -> None:
            return None

        with pytest.raises(RuntimeError):
            await breaker.call(boom)
        await asyncio.sleep(0.06)
        await breaker.call(ok)
        assert transitions == [
            ("closed", "open"),
            ("open", "half_open"),
            ("half_open", "closed"),
        ]

    async def test_rejects_bad_config(self) -> None:
        with pytest.raises(ValueError):
            CircuitBreaker(name="t", failure_threshold=0)
        with pytest.raises(ValueError):
            CircuitBreaker(name="t", reset_timeout_seconds=0)


class TestCircuitBreakerRegistry:
    async def test_reuses_breaker_by_name(self) -> None:
        registry = CircuitBreakerRegistry()
        first = registry.get_or_create("yahoo")
        second = registry.get_or_create("yahoo")
        assert first is second
        assert registry.snapshots() == [first.snapshot()]

    async def test_snapshots_sorted(self) -> None:
        registry = CircuitBreakerRegistry()
        registry.get_or_create("broker", failure_threshold=2)
        registry.get_or_create("yahoo")
        names = [s["name"] for s in registry.snapshots()]
        assert names == ["broker", "yahoo"]


class TestTokenBucket:
    async def test_budget_enforced(self) -> None:
        bucket = TokenBucket(capacity=2, refill_rate_per_second=0.0)
        assert await bucket.acquire() is True
        assert await bucket.acquire() is True
        assert await bucket.acquire() is False

    async def test_refill_over_time(self) -> None:
        bucket = TokenBucket(capacity=1, refill_rate_per_second=100.0)
        assert await bucket.acquire() is True
        await asyncio.sleep(0.03)
        assert await bucket.acquire() is True

    async def test_wait_blocks_until_tokens_available(self) -> None:
        bucket = TokenBucket(capacity=1, refill_rate_per_second=1000.0)
        await bucket.acquire()
        started = asyncio.get_running_loop().time()
        await bucket.wait()
        assert asyncio.get_running_loop().time() - started < 1.0

    async def test_bad_config(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=0, refill_rate_per_second=1)
        with pytest.raises(ValueError):
            TokenBucket(capacity=1, refill_rate_per_second=-1)


class TestRetry:
    async def test_retries_transient_then_succeeds(self) -> None:
        calls = 0

        @retry_async(attempts=3)
        async def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ConnectError("no route")
            return "ok"

        assert await flaky() == "ok"
        assert calls == 3

    async def test_gives_up_after_attempts(self) -> None:
        calls = 0

        @retry_async(attempts=2)
        async def always_down() -> None:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("no route")

        with pytest.raises(httpx.ConnectError):
            await always_down()
        assert calls == 2

    async def test_does_not_retry_client_errors(self) -> None:
        calls = 0

        @retry_async(attempts=3)
        async def four_oh_four() -> None:
            nonlocal calls
            calls += 1
            raise _status_error(404)

        with pytest.raises(httpx.HTTPStatusError):
            await four_oh_four()
        assert calls == 1
