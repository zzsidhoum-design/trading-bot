"""Agent registry — maps agent name → class for the CLI and wiring."""

from __future__ import annotations

from qtrader.application.agents.base import AgentBase


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, type[AgentBase]] = {}

    def register(self, agent: type[AgentBase]) -> None:
        self._agents[agent.name] = agent

    def get(self, name: str) -> type[AgentBase] | None:
        return self._agents.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._agents)


def default_registry() -> AgentRegistry:
    """All agents built in this phase, importable by the CLI."""
    from qtrader.application.agents.data import DataAgent
    from qtrader.application.agents.scanner import MarketScanner

    registry = AgentRegistry()
    registry.register(DataAgent)
    registry.register(MarketScanner)
    return registry
