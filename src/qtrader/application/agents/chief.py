"""Chief Agent — orchestrator producing the final BUY/SELL/HOLD (docs/02-agents.md §10).

Collects the latest evidence per candidate (technical/news/fundamental signals
+ prediction), runs the pluggable ``DecisionStrategy`` (weighted ensemble),
persists an explainable ``DecisionRecord`` and publishes ``DecisionMade`` for
any non-HOLD outcome (which feeds the Risk gate in a later phase).
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import ClassVar

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.domain.entities import AgentEvidence, DecisionRecord
from qtrader.domain.events import DecisionMade, DomainEvent, ScanCompleted
from qtrader.domain.ports import (
    DecisionRepository,
    DecisionStrategy,
    EventBus,
    PredictionRepository,
    SignalRepository,
)
from qtrader.domain.value_objects import Decision

SCORE_QUANT = Decimal("0.0001")

EVIDENCE_AGENTS: tuple[str, ...] = ("technical", "news", "fundamental")


def _dec(value: float) -> Decimal:
    return Decimal(str(value)).quantize(SCORE_QUANT, rounding=ROUND_HALF_UP)


class ChiefAgent(AgentBase):
    name: ClassVar[str] = "chief"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (ScanCompleted,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (DecisionMade,)

    def __init__(
        self,
        signals: SignalRepository,
        predictions: PredictionRepository,
        decisions: DecisionRepository,
        bus: EventBus,
        strategy: DecisionStrategy,
    ) -> None:
        self._signals = signals
        self._predictions = predictions
        self._decisions = decisions
        self._bus = bus
        self._strategy = strategy

    async def decide_symbol(self, symbol: str) -> DecisionRecord | None:
        evidence: list[AgentEvidence] = []
        for agent in EVIDENCE_AGENTS:
            latest = await self._signals.latest_for_symbol(symbol, agent)
            if not latest:
                continue
            signal = latest[0]
            evidence.append(
                AgentEvidence(
                    agent=agent,
                    score=float(signal.score),
                    reason=f"{signal.signal_type.value} {float(signal.score):+.3f}",
                )
            )

        predictions = await self._predictions.latest_for_symbol(symbol, limit=1)
        if predictions:
            pred = predictions[0]
            prob_up = float(pred.prob_up or 0.0)
            prob_down = float(pred.prob_down or 0.0)
            evidence.append(
                AgentEvidence(
                    agent="prediction",
                    score=prob_up - prob_down,
                    reason=(
                        f"model={pred.model_name} v{pred.model_version} "
                        f"conf={float(pred.confidence or 0.0):.2f} "
                        f"exp_ret={float(pred.expected_return or 0.0):+.4f}"
                    ),
                )
            )

        if not evidence:
            self._logger.warning("chief.no_evidence", symbol=symbol)
            return None

        outcome = self._strategy.decide(evidence)
        record = DecisionRecord(
            decision_uuid=str(uuid.uuid4()),
            symbol=symbol,
            decision=outcome.decision,
            confidence=_dec(outcome.confidence),
            rationale=outcome.rationale,
            agent_scores=outcome.agent_scores,
        )
        await self._decisions.save(record)
        self._logger.info(
            "chief.decision",
            symbol=symbol,
            decision=outcome.decision,
            confidence=outcome.confidence,
        )
        if outcome.decision is not Decision.HOLD:
            await self._bus.publish(
                DecisionMade(
                    decision_uuid=record.decision_uuid,
                    symbol=symbol,
                    decision=outcome.decision,
                    confidence=outcome.confidence,
                    rationale=outcome.rationale,
                    agent_scores=outcome.agent_scores,
                )
            )
        return record

    async def decide_candidates(self, symbols: list[str]) -> int:
        decided = 0
        for symbol in symbols:
            try:
                if await self.decide_symbol(symbol) is not None:
                    decided += 1
            except Exception:
                self._logger.exception("chief.decide_failed", symbol=symbol)
        return decided

    async def on_event(self, event: DomainEvent) -> None:
        if isinstance(event, ScanCompleted):
            await self.decide_candidates([c["symbol"] for c in event.candidates])

    async def run(self, ctx: AgentContext) -> None:
        await self.decide_symbol(ctx.symbol)
