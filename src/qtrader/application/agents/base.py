"""Agent base contract (see docs/02-agents.md).

Agents are stateless workers that communicate only through typed domain
events on the EventBus. They never touch infrastructure directly — all
dependencies arrive via constructor injection (ports).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

import structlog

from qtrader.config.logging import get_logger
from qtrader.domain.events import DomainEvent
from qtrader.domain.value_objects import Interval


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Standalone invocation context (used by the CLI / scheduler)."""

    symbol: str
    interval: Interval = Interval.M5
    start: datetime | None = None
    end: datetime | None = None


class AgentBase(ABC):
    name: ClassVar[str]
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = ()
    produces: ClassVar[tuple[type[DomainEvent], ...]] = ()

    @property
    def _logger(self) -> structlog.stdlib.BoundLogger:
        return get_logger(f"qtrader.agent.{self.name}")

    @abstractmethod
    async def run(self, ctx: AgentContext) -> None:
        """Standalone entry point (CLI ``qtrader run-agent``)."""

    async def on_event(self, event: DomainEvent) -> None:  # noqa: B027
        """Event-driven entry point; default is a no-op."""

    async def run_batch(
        self,
        symbols: list[str],
        per_symbol: Callable[[str], Awaitable[object]],
        *,
        action: str,
    ) -> int:
        """Run ``per_symbol`` for every symbol, tolerating individual failures.

        A failed symbol is logged and skipped so one bad symbol never aborts
        the whole scan. Returns the number of symbols that produced a usable
        result (non-None and non-zero).
        """
        done = 0
        for symbol in symbols:
            try:
                result = await per_symbol(symbol)
            except Exception:
                self._logger.exception(action, symbol=symbol)
                continue
            if result is not None and result != 0.0:
                done += 1
        return done
