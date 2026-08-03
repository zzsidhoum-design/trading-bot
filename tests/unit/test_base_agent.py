"""Unit tests for the AgentBase contract (batch helper, event contract)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.domain.events import DomainEvent, PriceUpdated


class _FakeEvent(DomainEvent):
    pass


class _TestAgent(AgentBase):
    name: ClassVar[str] = "test"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (_FakeEvent,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (PriceUpdated,)

    def __init__(
        self,
        fail_symbols: set[str] | None = None,
        zero_symbols: set[str] | None = None,
    ) -> None:
        self._fail = fail_symbols or set()
        self._zero = zero_symbols or set()

    async def run(self, ctx: AgentContext) -> None:
        return None

    async def _analyze(self, symbol: str) -> object:
        if symbol in self._fail:
            raise RuntimeError("boom")
        if symbol in self._zero:
            return 0.0
        return f"ok:{symbol}"


@pytest.mark.asyncio
async def test_run_batch_counts_usable_results() -> None:
    agent = _TestAgent(fail_symbols={"B"}, zero_symbols={"C"})
    done = await agent.run_batch(["A", "B", "C", "D"], agent._analyze, action="test.failed")
    assert done == 2


@pytest.mark.asyncio
async def test_run_batch_tolerates_failures(capsys) -> None:
    from qtrader.config.logging import configure_logging

    configure_logging()
    agent = _TestAgent(fail_symbols={"B"})
    done = await agent.run_batch(["A", "B"], agent._analyze, action="test.failed")
    assert done == 1
    captured = capsys.readouterr()
    assert "test.failed" in captured.err
    assert "RuntimeError: boom" in captured.err


@pytest.mark.asyncio
async def test_run_batch_empty() -> None:
    agent = _TestAgent()
    assert await agent.run_batch([], agent._analyze, action="test.failed") == 0


def test_agent_contract_metadata() -> None:
    assert _TestAgent.name == "test"
    assert _TestAgent.consumes == (_FakeEvent,)
    assert _TestAgent.produces == (PriceUpdated,)
    assert issubclass(_TestAgent, AgentBase)
