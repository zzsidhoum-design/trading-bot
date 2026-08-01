"""Unit tests for domain events (identity, typing, serialization)."""

from __future__ import annotations

from decimal import Decimal

from qtrader.domain import events
from qtrader.domain.events import (
    DecisionMade,
    DomainEvent,
    OrderPlan,
    PriceUpdated,
    RiskApproved,
)
from qtrader.domain.value_objects import Decision, Interval, OrderType, Percentage, TradeSide


class TestDomainEvents:
    def test_unique_uuids(self) -> None:
        assert (
            PriceUpdated(
                symbol="AAPL",
                interval=Interval.M1,
                ts="2026-08-01T12:00:00Z",
                open="1",
                high="1",
                low="1",
                close="1",
                volume="1",
            ).event_uuid
            != PriceUpdated(
                symbol="AAPL",
                interval=Interval.M1,
                ts="2026-08-01T12:00:00Z",
                open="1",
                high="1",
                low="1",
                close="1",
                volume="1",
            ).event_uuid
        )

    def test_type_name(self) -> None:
        event: DomainEvent = PriceUpdated(
            symbol="AAPL",
            interval=Interval.M1,
            ts="2026-08-01T12:00:00Z",
            open="1",
            high="1",
            low="1",
            close="1",
            volume="1",
        )
        assert event.type_name == "PriceUpdated"

    def test_payload_is_json_serializable(self) -> None:
        event = DecisionMade(
            decision_uuid="abc",
            symbol="AAPL",
            decision=Decision.BUY,
            confidence=0.8,
            rationale="bullish",
            agent_scores={"technical": 0.7},
        )
        payload = event.payload()
        assert payload["decision"] == "BUY"
        assert payload["agent_scores"] == {"technical": 0.7}

    def test_risk_approved_payload(self) -> None:
        plan = OrderPlan(
            symbol="AAPL",
            side=TradeSide.BUY,
            quantity=Decimal("10"),
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_loss=Decimal("90"),
            take_profit=Decimal("120"),
            risk_per_trade=Percentage("0.01"),
            estimated_exposure=Percentage("0.05"),
        )
        event = RiskApproved(decision_uuid="abc", plan=plan)
        assert event.payload()["plan"]["symbol"] == "AAPL"

    def test_events_module_exposes_catalog(self) -> None:
        for name in [
            "PriceUpdated",
            "BackfillCompleted",
            "ScanCompleted",
            "SignalGenerated",
            "NewsSignalGenerated",
            "PredictionGenerated",
            "DecisionMade",
            "RiskApproved",
            "RiskRejected",
            "OrderSubmitted",
            "OrderFilled",
            "OrderStatusChanged",
            "PositionClosed",
            "AgentError",
        ]:
            assert hasattr(events, name), f"missing event {name}"
