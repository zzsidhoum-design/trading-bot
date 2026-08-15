"""PaperTradingService — orchestration for the paper-trading layer.

Routes a Chief decision (or raw order intent) into the paper environment with:

* **Duplicate protection** — re-delivering the same ``decision_ref`` returns the
  existing record instead of re-submitting (crash + redelivery safety).
* **Risk attribution** — the PortfolioRiskEngine verdict is recorded on the
  order record so intervention statistics can be produced.
* **Recovery** — :meth:`recover` reloads the ledger after a process restart and
  re-polls every SUBMITTED order exactly once; it never creates new orders, so
  a restart cannot double-trade.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from qtrader.application.paper.brokers import ShadowBroker
from qtrader.application.paper.ledger import PaperOrderLedger
from qtrader.application.paper.models import (
    PaperOrderRecord,
    PaperOrderStatus,
    RiskInterventionStats,
)
from qtrader.application.paper.telemetry import TelemetryRecorder
from qtrader.domain.entities import Order
from qtrader.domain.ports import BrokerGateway
from qtrader.domain.value_objects import OrderFill, OrderStatus


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Result of a recovery pass over stale SUBMITTED orders."""

    reloaded: int
    repolled: int
    filled: int
    still_pending: int
    failed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "reloaded": self.reloaded,
            "repolled": self.repolled,
            "filled": self.filled,
            "still_pending": self.still_pending,
            "failed": self.failed,
        }


class PaperTradingService:
    """Orchestrates paper decisions, recovery and risk statistics."""

    def __init__(
        self,
        *,
        ledger: PaperOrderLedger,
        telemetry: TelemetryRecorder,
        broker: BrokerGateway,
        shadow: bool = False,
        default_price: Decimal = Decimal("100"),
    ) -> None:
        self._ledger = ledger
        self._telemetry = telemetry
        self._broker = broker
        self._shadow = shadow
        self._default_price = default_price

    @property
    def shadow(self) -> bool:
        return self._shadow

    async def route_decision(
        self,
        *,
        decision_ref: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal | int,
        strategy: str = "default",
        requested_price: Decimal | None = None,
        risk_verdict: str | None = None,
        risk_reason: str | None = None,
        agent: str | None = None,
        order: Order | None = None,
        context: dict | None = None,
    ) -> PaperOrderRecord:
        """Record and (unless shadow mode) execute one decision.

        Re-delivery of an existing, non-terminal decision is suppressed — the
        existing record is returned and the broker is never called again.
        """
        existing = self._ledger.get_by_decision_ref(decision_ref)
        if existing is not None and existing.status is not PaperOrderStatus.REJECTED:
            await self._telemetry.record_log(
                "INFO",
                "duplicate decision suppressed",
                component="paper.service",
                context={"decision_ref": decision_ref, "status": existing.status.value},
            )
            return existing

        context = dict(context or {})
        if agent:
            context["agent"] = agent
        fields: dict[str, Any] = {
            "decision_ref": decision_ref,
            "asset": symbol,
            "side": side,
            "order_type": order_type,
            "quantity": Decimal(quantity),
            "requested_price": requested_price,
            "strategy": strategy,
            "risk_verdict": risk_verdict,
            "risk_reason": risk_reason,
            "context": context,
        }

        if self._shadow or isinstance(self._broker, ShadowBroker):
            simulated = requested_price or self._default_price
            record = self._ledger.update(
                decision_ref,
                status=PaperOrderStatus.SHADOW_ONLY,
                shadow=True,
                simulated_price=simulated,
                **fields,
            )
            await self._telemetry.signal_frequency(agent or strategy)
            return record

        record = self._ledger.update(
            decision_ref, status=PaperOrderStatus.PROPOSED, **fields
        )
        await self._telemetry.signal_frequency(agent or strategy)

        if order is None:
            return record

        if order.decision_ref is None:
            order = replace(order, decision_ref=decision_ref)
        try:
            broker_order_id = await self._broker.submit_order(order)
        except Exception as exc:  # noqa: BLE001 - rejection is a recorded outcome
            await self._telemetry.record_log(
                "WARN",
                f"order rejected: {exc}",
                component="paper.service",
                context={"decision_ref": decision_ref},
            )
            self._ledger.update(
                decision_ref,
                status=PaperOrderStatus.REJECTED,
                rejection_reason=str(exc),
            )
            return self._ledger.get_by_decision_ref(decision_ref) or record

        fill = await self._broker.get_order_status(broker_order_id)
        self._apply_fill(decision_ref, fill)
        return self._ledger.get_by_decision_ref(decision_ref) or record

    def _apply_fill(self, decision_ref: str, fill: OrderFill) -> None:
        """Reconcile the ledger from a broker OrderFill (idempotent merge)."""
        if fill.status is OrderStatus.FILLED:
            final_status = PaperOrderStatus.FILLED
        elif fill.status is OrderStatus.PARTIAL:
            final_status = PaperOrderStatus.PARTIAL
        else:
            return
        slippage = None
        record = self._ledger.get_by_decision_ref(decision_ref)
        if (
            fill.avg_fill_price is not None
            and record is not None
            and record.requested_price is not None
        ):
            slippage = fill.avg_fill_price - record.requested_price
        self._ledger.update(
            decision_ref,
            status=final_status,
            fill_price=fill.avg_fill_price,
            slippage=slippage,
        )

    async def recover(self) -> RecoveryReport:
        """Re-poll stale SUBMITTED orders after a restart; never duplicates."""
        stale = self._ledger.stale()
        filled = 0
        still_pending = 0
        failed = 0
        for record in stale:
            if record.broker_order_id is None:
                still_pending += 1
                continue
            try:
                fill = await self._broker.get_order_status(record.broker_order_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                await self._telemetry.record_log(
                    "WARN",
                    f"recovery poll failed: {exc}",
                    component="paper.service",
                    context={"broker_order_id": record.broker_order_id},
                )
                continue
            if fill.status.value in ("FILLED", "PARTIAL"):
                filled += 1
            else:
                still_pending += 1
        if self._ledger.path is not None:
            self._ledger.write()
        return RecoveryReport(
            reloaded=self._ledger.count(),
            repolled=len(stale),
            filled=filled,
            still_pending=still_pending,
            failed=failed,
        )

    def risk_intervention_stats(self) -> RiskInterventionStats:
        """Risk-engine intervention statistics (required output #7)."""
        records = self._ledger.all()
        verdicts = [r.risk_verdict for r in records if r.risk_verdict is not None]
        reasons: dict[str, int] = {}
        for record in records:
            if record.risk_verdict in ("rejected", "capped") and record.risk_reason:
                reason = record.risk_reason
                reasons[reason] = reasons.get(reason, 0) + 1
        evaluated = len(verdicts)
        approved = verdicts.count("approved")
        capped = verdicts.count("capped")
        rejected = verdicts.count("rejected")
        intervention_rate = (capped + rejected) / evaluated if evaluated else 0.0
        return RiskInterventionStats(
            decisions_evaluated=evaluated,
            approved=approved,
            capped=capped,
            rejected=rejected,
            intervention_rate=intervention_rate,
            reasons=reasons,
        )

    @property
    def ledger(self) -> PaperOrderLedger:
        return self._ledger


__all__ = ["PaperTradingService", "RecoveryReport"]
