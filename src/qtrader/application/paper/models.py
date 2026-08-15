"""Phase 7 — paper-trading domain records.

Every proposed/simulated order, its risk verdict and its execution outcome is
captured as an immutable :class:`PaperOrderRecord`. The record carries every
field the audit requires (timestamp, asset, side, quantity, order type,
requested price, simulated/paper price, slippage, status, rejection reason and
execution latency) plus the risk-engineering verdict so that risk-engine
intervention statistics can be produced from the same ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


class PaperOrderStatus(StrEnum):
    """Lifecycle of a paper order as recorded in the ledger.

    ``PROPOSED`` is the decision before submission, ``SUBMITTED`` was handed to
    the (paper) broker, ``SHADOW_ONLY`` is a decision that was deliberately
    never submitted (shadow mode) and ``REJECTED`` was refused by the broker or
    a pre-trade control.
    """

    PROPOSED = "proposed"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    SHADOW_ONLY = "shadow_only"


@dataclass(frozen=True, slots=True)
class PaperOrderRecord:
    """One auditable paper order/decision record."""

    key: str
    asset: str = ""
    side: str = "BUY"
    quantity: Decimal = Decimal("0")
    order_type: str = "MARKET"
    timestamp: datetime = field(default_factory=_now)
    requested_price: Decimal | None = None
    simulated_price: Decimal | None = None
    fill_price: Decimal | None = None
    slippage: Decimal | None = None
    status: PaperOrderStatus = PaperOrderStatus.PROPOSED
    rejection_reason: str | None = None
    execution_latency_ms: float | None = None
    broker_order_id: str | None = None
    strategy: str = "default"
    decision_ref: str | None = None
    shadow: bool = False
    risk_verdict: str | None = None
    risk_reason: str | None = None
    commission: Decimal = Decimal("0")
    context: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            PaperOrderStatus.FILLED,
            PaperOrderStatus.CANCELED,
            PaperOrderStatus.REJECTED,
            PaperOrderStatus.SHADOW_ONLY,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "asset": self.asset,
            "side": self.side,
            "quantity": str(self.quantity),
            "order_type": self.order_type,
            "timestamp": self.timestamp.isoformat(),
            "requested_price": (
                str(self.requested_price) if self.requested_price is not None else None
            ),
            "simulated_price": (
                str(self.simulated_price) if self.simulated_price is not None else None
            ),
            "fill_price": str(self.fill_price) if self.fill_price is not None else None,
            "slippage": str(self.slippage) if self.slippage is not None else None,
            "status": self.status.value,
            "rejection_reason": self.rejection_reason,
            "execution_latency_ms": self.execution_latency_ms,
            "broker_order_id": self.broker_order_id,
            "strategy": self.strategy,
            "decision_ref": self.decision_ref,
            "shadow": self.shadow,
            "risk_verdict": self.risk_verdict,
            "risk_reason": self.risk_reason,
            "commission": str(self.commission),
            "context": dict(self.context),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperOrderRecord:
        price_fields = ("requested_price", "simulated_price", "fill_price", "slippage")
        prices = {name: data.get(name) for name in price_fields}
        return cls(
            key=str(data["key"]),
            asset=str(data["asset"]),
            side=str(data["side"]),
            quantity=Decimal(str(data["quantity"])),
            order_type=str(data["order_type"]),
            timestamp=datetime.fromisoformat(str(data["timestamp"])),
            requested_price=(
                Decimal(prices["requested_price"])
                if prices["requested_price"] is not None
                else None
            ),
            simulated_price=(
                Decimal(prices["simulated_price"])
                if prices["simulated_price"] is not None
                else None
            ),
            fill_price=(
                Decimal(prices["fill_price"]) if prices["fill_price"] is not None else None
            ),
            slippage=(
                Decimal(prices["slippage"]) if prices["slippage"] is not None else None
            ),
            status=PaperOrderStatus(str(data["status"])),
            rejection_reason=data.get("rejection_reason"),
            execution_latency_ms=data.get("execution_latency_ms"),
            broker_order_id=data.get("broker_order_id"),
            strategy=str(data.get("strategy", "default")),
            decision_ref=data.get("decision_ref"),
            shadow=bool(data.get("shadow", False)),
            risk_verdict=data.get("risk_verdict"),
            risk_reason=data.get("risk_reason"),
            commission=Decimal(str(data.get("commission", "0"))),
            context=dict(data.get("context", {})),
        )


@dataclass(frozen=True, slots=True)
class PaperRunStats:
    """Aggregate execution statistics over a ledger (required output #2)."""

    total_orders: int
    proposed: int
    submitted: int
    filled: int
    partial: int
    canceled: int
    rejected: int
    shadow_only: int
    fill_rate: float
    avg_slippage_bps: float
    avg_execution_latency_ms: float
    total_commission: Decimal
    risk_approved: int
    risk_capped: int
    risk_rejected: int
    risk_not_gated: int
    earliest: datetime | None
    latest: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_orders": self.total_orders,
            "proposed": self.proposed,
            "submitted": self.submitted,
            "filled": self.filled,
            "partial": self.partial,
            "canceled": self.canceled,
            "rejected": self.rejected,
            "shadow_only": self.shadow_only,
            "fill_rate": self.fill_rate,
            "avg_slippage_bps": self.avg_slippage_bps,
            "avg_execution_latency_ms": self.avg_execution_latency_ms,
            "total_commission": str(self.total_commission),
            "risk_approved": self.risk_approved,
            "risk_capped": self.risk_capped,
            "risk_rejected": self.risk_rejected,
            "risk_not_gated": self.risk_not_gated,
            "earliest": self.earliest.isoformat() if self.earliest else None,
            "latest": self.latest.isoformat() if self.latest else None,
        }


@dataclass(frozen=True, slots=True)
class RiskInterventionStats:
    """Risk-engine intervention statistics (required output #7)."""

    decisions_evaluated: int
    approved: int
    capped: int
    rejected: int
    intervention_rate: float
    reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions_evaluated": self.decisions_evaluated,
            "approved": self.approved,
            "capped": self.capped,
            "rejected": self.rejected,
            "intervention_rate": self.intervention_rate,
            "reasons": dict(self.reasons),
        }


__all__ = [
    "PaperOrderRecord",
    "PaperOrderStatus",
    "PaperRunStats",
    "RiskInterventionStats",
]
