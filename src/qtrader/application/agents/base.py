"""Agent base contract (see docs/02-agents.md).

Agents are stateless workers that communicate only through typed domain
events on the EventBus. They never touch infrastructure directly — all
dependencies arrive via constructor injection (ports).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

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

    @abstractmethod
    async def run(self, ctx: AgentContext) -> None:
        """Standalone entry point (CLI ``qtrader run-agent``)."""

    async def on_event(self, event: DomainEvent) -> None:  # noqa: B027
        """Event-driven entry point; default is a no-op."""
