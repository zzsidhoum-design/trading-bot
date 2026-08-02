"""Typed domain events used as the only communication channel between agents.

Every event carries a unique ``event_uuid`` (idempotency) and an UTC timestamp.
The type name (``type_name``) is used for the outbox ``events.type`` column and
for Redis pub/sub routing.

Field-ordering note: dataclass inheritance requires non-default fields first.
We therefore split the metadata fields into ``EventMeta`` (with defaults) which
every concrete event inherits *after* its own fields, keeping the generated
``__init__`` valid for all subclasses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from qtrader.domain.value_objects import (
    Decision,
    Interval,
    MarketImpact,
    OrderPlan,
    OrderStatus,
    SignalType,
    TradeSide,
    TradingMode,
)


def _now() -> datetime:
    return datetime.now(UTC)


class DomainEvent:
    """Base contract: every event carries metadata and a serializable payload.

    The concrete fields are provided by the ``EventMeta`` mixin; the
    annotations here exist only so static checkers see the attributes.
    """

    event_uuid: str
    occurred_at: datetime

    @property
    def type_name(self) -> str:
        return type(self).__name__

    def payload(self) -> dict[str, Any]:
        """JSON-serializable payload for the outbox / pub-sub transport."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True, kw_only=True)
class EventMeta:
    event_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Data ingestion
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, kw_only=True)
class PriceUpdated(DomainEvent, EventMeta):
    symbol: str
    interval: Interval
    ts: str
    open: str
    high: str
    low: str
    close: str
    volume: str

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class BackfillCompleted(DomainEvent, EventMeta):
    symbol: str
    interval: Interval
    start: str
    end: str

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "start": self.start,
            "end": self.end,
        }


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanCompleted(DomainEvent, EventMeta):
    candidates: list[dict[str, Any]]

    def payload(self) -> dict[str, Any]:
        return {"candidates": self.candidates}


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalGenerated(DomainEvent, EventMeta):
    symbol: str
    agent: str
    signal_type: SignalType
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "agent": self.agent,
            "signal_type": self.signal_type,
            "score": self.score,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NewsSignalGenerated(SignalGenerated):
    impact: MarketImpact = MarketImpact.LOW
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class TechnicalSignalGenerated(SignalGenerated):
    interval: Interval
    sub_scores: dict[str, float] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "agent": self.agent,
            "signal_type": self.signal_type,
            "score": self.score,
            "interval": self.interval,
            "sub_scores": self.sub_scores,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FundamentalSignalGenerated(SignalGenerated):
    rating: str
    as_of: str
    sub_scores: dict[str, float] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "agent": self.agent,
            "signal_type": self.signal_type,
            "score": self.score,
            "rating": self.rating,
            "as_of": self.as_of,
            "sub_scores": self.sub_scores,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PredictionGenerated(DomainEvent, EventMeta):
    symbol: str
    model_name: str
    prob_up: float
    prob_down: float
    prob_trend: float
    confidence: float
    expected_return: float

    def payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "model_name": self.model_name,
            "prob_up": self.prob_up,
            "prob_down": self.prob_down,
            "prob_trend": self.prob_trend,
            "confidence": self.confidence,
            "expected_return": self.expected_return,
        }


# --------------------------------------------------------------------------- #
# Decision & risk
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionMade(DomainEvent, EventMeta):
    decision_uuid: str
    symbol: str
    decision: Decision
    confidence: float
    rationale: str
    agent_scores: dict[str, float] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "decision_uuid": self.decision_uuid,
            "symbol": self.symbol,
            "decision": self.decision,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "agent_scores": self.agent_scores,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskApproved(DomainEvent, EventMeta):
    decision_uuid: str
    plan: OrderPlan

    def payload(self) -> dict[str, Any]:
        p = self.plan
        return {
            "decision_uuid": self.decision_uuid,
            "plan": {
                "symbol": p.symbol,
                "side": p.side,
                "quantity": str(p.quantity),
                "order_type": p.order_type,
                "limit_price": str(p.limit_price) if p.limit_price is not None else None,
                "stop_loss": str(p.stop_loss),
                "take_profit": str(p.take_profit),
                "risk_per_trade": str(p.risk_per_trade.value),
                "estimated_exposure": str(p.estimated_exposure.value),
                "entry_price": str(p.entry_price),
            },
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskRejected(DomainEvent, EventMeta):
    decision_uuid: str
    symbol: str
    reasons: list[str]

    def payload(self) -> dict[str, Any]:
        return {"decision_uuid": self.decision_uuid, "symbol": self.symbol, "reasons": self.reasons}


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, kw_only=True)
class AllocationProposal(DomainEvent, EventMeta):
    """Portfolio Agent → Execution Agent: a risk-approved order to submit."""

    decision_uuid: str
    order_id: str
    symbol: str
    side: TradeSide
    quantity: str
    order_type: str
    mode: TradingMode
    stop_loss: str | None = None
    take_profit: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "decision_uuid": self.decision_uuid,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "mode": self.mode,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderSubmitted(DomainEvent, EventMeta):
    order_id: str
    symbol: str
    side: TradeSide
    quantity: str
    order_type: str
    mode: TradingMode

    def payload(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderFilled(DomainEvent, EventMeta):
    order_id: str
    broker_order_id: str
    fill_price: str
    fill_qty: str
    fees: str

    def payload(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "broker_order_id": self.broker_order_id,
            "fill_price": self.fill_price,
            "fill_qty": self.fill_qty,
            "fees": self.fees,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderStatusChanged(DomainEvent, EventMeta):
    order_id: str
    status: OrderStatus
    detail: str = ""

    def payload(self) -> dict[str, Any]:
        return {"order_id": self.order_id, "status": self.status, "detail": self.detail}


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionClosed(DomainEvent, EventMeta):
    position_id: str
    symbol: str
    pnl: str
    pnl_pct: str

    def payload(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
        }


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentError(DomainEvent, EventMeta):
    agent: str
    error: str
    context: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {"agent": self.agent, "error": self.error, "context": self.context}
