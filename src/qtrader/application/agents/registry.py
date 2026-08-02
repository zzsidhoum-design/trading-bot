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
    from qtrader.application.agents.chief import ChiefAgent
    from qtrader.application.agents.data import DataAgent
    from qtrader.application.agents.execution import ExecutionAgent
    from qtrader.application.agents.fundamental import FundamentalAgent
    from qtrader.application.agents.news import NewsAgent
    from qtrader.application.agents.portfolio import PortfolioAgent
    from qtrader.application.agents.prediction import PredictionAgent
    from qtrader.application.agents.risk import RiskAgent
    from qtrader.application.agents.scanner import MarketScanner
    from qtrader.application.agents.technical import TechnicalAgent

    registry = AgentRegistry()
    registry.register(DataAgent)
    registry.register(MarketScanner)
    registry.register(TechnicalAgent)
    registry.register(NewsAgent)
    registry.register(FundamentalAgent)
    registry.register(PredictionAgent)
    registry.register(ChiefAgent)
    registry.register(RiskAgent)
    registry.register(PortfolioAgent)
    registry.register(ExecutionAgent)
    return registry
