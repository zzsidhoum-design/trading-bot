"""Async retry helpers built on tenacity (exponential backoff + jitter).

Adapters apply :func:`retry_async` to external calls and pass
:func:`is_transient` (or their own predicate) so only transient failures are
retried — 4xx client errors surface immediately.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, cast

from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)

T = TypeVar("T")

TRANSIENT_HTTP_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
)


def is_transient(exc: BaseException) -> bool:
    """True for network hiccups and 5xx/429 HTTP responses.

    httpx is imported lazily so this module has no hard dependency on a
    specific HTTP client.
    """
    import httpx

    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.TransportError,
            *TRANSIENT_HTTP_EXCEPTIONS,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


def retry_if_transient(retry_state: Any) -> bool:
    return is_transient(retry_state.outcome.exception())


def retry_async(
    *,
    attempts: int = 3,
    min_wait_seconds: float = 0.05,
    max_wait_seconds: float = 2.0,
    retry_on: Callable[[BaseException], bool] = is_transient,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory: async retry with exponential backoff + jitter.

    Only exceptions accepted by ``retry_on`` are retried; others propagate.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        return cast(
            Callable[..., Any],
            retry(
                stop=stop_after_attempt(attempts),
                wait=wait_random_exponential(
                    multiplier=min_wait_seconds,
                    max=max_wait_seconds,
                    exp_base=2,
                ),
                retry=retry_if_exception(retry_on),
                reraise=True,
            )(fn),
        )

    return decorate


def retry_if_exception(predicate: Callable[[BaseException], bool]) -> Any:
    """tenacity ``retry_base`` predicate built from a plain function."""
    from tenacity import retry_base

    class _RetryIf(retry_base):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()

        def __call__(self, retry_state: Any) -> bool:
            outcome = retry_state.outcome
            if outcome is None or outcome.exception() is None:
                return False
            return bool(predicate(outcome.exception()))

    return _RetryIf()
