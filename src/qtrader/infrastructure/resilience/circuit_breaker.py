"""Async circuit breaker.

State machine:

- ``CLOSED`` — calls pass through; consecutive failures counted.
- ``OPEN`` — calls fail fast (:class:`CircuitOpenError`) until the reset
  timeout elapses, then transition to ``HALF_OPEN``.
- ``HALF_OPEN`` — a bounded number of probe calls are allowed; a success
  closes the breaker, a failure reopens it.

Usage::

    breaker = CircuitBreaker(name="yahoo", failure_threshold=3, reset_timeout_seconds=5)
    try:
        data = await breaker.call(lambda: client.get(...))
    except CircuitOpenError:
        # degraded mode — skip, log, emit error event
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

StateChangeCallback = Callable[[str, str], None]


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the breaker is open."""


class CircuitBreaker:
    """Consecutive-failure circuit breaker for async callables.

    Thread-affine within a single event loop; not safe across loops.
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 30.0,
        half_open_max_attempts: int = 1,
        on_state_change: StateChangeCallback | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_timeout_seconds <= 0:
            raise ValueError("reset_timeout_seconds must be > 0")
        self.name = name
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout_seconds
        self._half_open_max = half_open_max_attempts
        self._on_state_change = on_state_change

        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_attempts = 0

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    async def call(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run ``fn`` under the breaker; translate state transitions."""
        if self._state is BreakerState.CLOSED:
            return await self._call_closed(fn)
        return await self._call_open(fn)

    async def _call_closed(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            result = await fn()
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._open()
            logger.warning(
                "circuit_breaker.failure",
                breaker=self.name,
                failures=self._consecutive_failures,
                error=str(exc),
            )
            raise
        self._reset()
        return result

    async def _call_open(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        now = time.monotonic()
        if self._state is BreakerState.OPEN:
            assert self._opened_at is not None
            if now - self._opened_at < self._reset_timeout:
                raise CircuitOpenError(
                    f"breaker '{self.name}' open until "
                    f"{self._opened_at + self._reset_timeout - now:.1f}s"
                )
            self._transition(BreakerState.OPEN, BreakerState.HALF_OPEN)
            self._half_open_attempts = 0

        if self._half_open_attempts >= self._half_open_max:
            raise CircuitOpenError(f"breaker '{self.name}' half-open, probes exhausted")

        self._half_open_attempts += 1
        try:
            result = await fn()
        except Exception as exc:
            self._open()
            logger.warning(
                "circuit_breaker.probe_failed", breaker=self.name, error=str(exc)
            )
            raise
        self._reset()
        return result

    def _reset(self) -> None:
        if self._state is not BreakerState.CLOSED:
            self._transition(self._state, BreakerState.CLOSED)
        self._consecutive_failures = 0
        self._half_open_attempts = 0
        self._opened_at = None

    def _open(self) -> None:
        if self._state is not BreakerState.OPEN:
            self._transition(self._state, BreakerState.OPEN)
        self._opened_at = time.monotonic()

    def _transition(self, source: BreakerState, target: BreakerState) -> None:
        self._state = target
        if self._on_state_change is not None:
            try:
                self._on_state_change(source.value, target.value)
            except Exception:  # never let observability break the circuit
                logger.exception("circuit_breaker.on_state_change_failed", breaker=self.name)

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "reset_timeout_seconds": self._reset_timeout,
        }


class CircuitBreakerRegistry:
    """Named collection of breakers for adapters + observability."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_or_create(self, name: str, **kwargs: Any) -> CircuitBreaker:
        breaker = self._breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(name=name, **kwargs)
            self._breakers[name] = breaker
        return breaker

    def snapshots(self) -> list[dict[str, Any]]:
        return sorted((b.snapshot() for b in self._breakers.values()), key=lambda s: s["name"])
