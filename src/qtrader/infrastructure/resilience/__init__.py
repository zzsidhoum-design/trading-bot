"""Resilience primitives for external adapters (Phase 8 hardening).

- :class:`CircuitBreaker` — fail fast when an external provider is down.
- :class:`CircuitBreakerRegistry` — named breakers + snapshot for observability.
- :class:`TokenBucket` — in-process async rate limiter.
- :func:`retry_async` — tenacity-based async retry with exponential backoff + jitter.
"""

from __future__ import annotations

from qtrader.infrastructure.resilience.circuit_breaker import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitOpenError,
)
from qtrader.infrastructure.resilience.rate_limiter import TokenBucket
from qtrader.infrastructure.resilience.retry import (
    is_transient,
    retry_async,
    retry_if_transient,
)

__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitOpenError",
    "TokenBucket",
    "is_transient",
    "retry_async",
    "retry_if_transient",
]
