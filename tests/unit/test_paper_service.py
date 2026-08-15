"""Unit tests for the Phase 7 PaperTradingService orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from qtrader.application.paper.ledger import PaperOrderLedger
from qtrader.application.paper.models import (
    PaperOrderRecord,
    PaperOrderStatus,
)
from qtrader.application.paper.service import PaperTradingService
from qtrader.application.paper.telemetry import OperationalTelemetry
from tests.unit.fakes_paper import (
    FakeAgentMetricRepository,
    FakeBroker,
    FakeRejectingBroker,
    FakeSystemLogRepository,
    make_order,
)


def _service(
    *,
    broker=None,
    shadow: bool = False,
    ledger: PaperOrderLedger | None = None,
) -> tuple[PaperTradingService, PaperOrderLedger, FakeAgentMetricRepository]:
    metrics = FakeAgentMetricRepository()
    telemetry = OperationalTelemetry(agent_metrics=metrics, logs=FakeSystemLogRepository())
    ledger = ledger or PaperOrderLedger()
    broker = broker or FakeBroker()
    return (
        PaperTradingService(
            ledger=ledger,
            telemetry=telemetry,
            broker=broker,
            shadow=shadow,
        ),
        ledger,
        metrics,
    )


async def test_route_decision_paper_submits_and_records_fill() -> None:
    service, ledger, _ = _service()
    record = await service.route_decision(
        decision_ref="dec-1",
        symbol="AAPL",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("10"),
        strategy="momentum",
        requested_price=Decimal("100.00"),
        risk_verdict="approved",
        order=make_order(decision_ref="dec-1"),
    )
    assert record.status is PaperOrderStatus.FILLED
    assert record.strategy == "momentum"
    assert record.risk_verdict == "approved"
    assert ledger.count() == 1


async def test_route_decision_duplicate_is_suppressed() -> None:
    inner = FakeBroker()
    service, ledger, _ = _service(broker=inner)
    await service.route_decision(
        decision_ref="dec-1",
        symbol="AAPL",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("10"),
        order=make_order(decision_ref="dec-1"),
    )
    second = await service.route_decision(
        decision_ref="dec-1",
        symbol="AAPL",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("10"),
        order=make_order(decision_ref="dec-1"),
    )
    assert second.status is PaperOrderStatus.FILLED
    assert len(inner.submitted) == 1
    assert ledger.count() == 1


async def test_route_decision_shadow_never_submits() -> None:
    inner = FakeBroker()
    service, ledger, _ = _service(broker=inner, shadow=True)
    record = await service.route_decision(
        decision_ref="dec-s",
        symbol="AAPL",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("10"),
        order=make_order(decision_ref="dec-s"),
    )
    assert record.status is PaperOrderStatus.SHADOW_ONLY
    assert record.shadow is True
    assert len(inner.submitted) == 0


async def test_route_decision_rejection_is_recorded() -> None:
    service, ledger, _ = _service(
        broker=FakeRejectingBroker(RuntimeError("no liquidity"))
    )
    record = await service.route_decision(
        decision_ref="dec-r",
        symbol="AAPL",
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("10"),
        order=make_order(decision_ref="dec-r"),
    )
    assert record.status is PaperOrderStatus.REJECTED
    assert record.rejection_reason == "no liquidity"


async def test_recover_repolls_stale_orders_without_duplicates(tmp_path) -> None:
    path = tmp_path / "orders.jsonl"
    ledger = PaperOrderLedger(path)
    ledger.record(
        PaperOrderRecord(
            key="dec-stale",
            decision_ref="dec-stale",
            asset="AAPL",
            side="BUY",
            quantity=Decimal("10"),
            order_type="MARKET",
            timestamp=datetime.now(UTC),
            status=PaperOrderStatus.SUBMITTED,
            broker_order_id="fake-9",
        )
    )
    ledger.write(path)

    restarted = PaperOrderLedger(path)
    service, _, _ = _service(ledger=restarted)
    report = await service.recover()

    assert report.reloaded == 1
    assert report.repolled == 1
    assert report.filled == 1
    assert report.still_pending == 0
    assert report.failed == 0
    assert restarted.count() == 1


async def test_recover_handles_poll_failures() -> None:
    ledger = PaperOrderLedger()
    ledger.record(
        PaperOrderRecord(
            key="dec-x",
            decision_ref="dec-x",
            asset="AAPL",
            side="BUY",
            quantity=Decimal("10"),
            order_type="MARKET",
            timestamp=datetime.now(UTC),
            status=PaperOrderStatus.SUBMITTED,
            broker_order_id="fake-broken",
        )
    )
    service, _, _ = _service(
        ledger=ledger,
        broker=FakeRejectingBroker(RuntimeError("down")),
    )
    report = await service.recover()
    assert report.failed == 1
    assert report.filled == 0


def test_risk_intervention_stats_counts_verdicts() -> None:
    ledger = PaperOrderLedger()
    ledger.record(
        PaperOrderRecord(
            key="1", decision_ref="1", asset="A", side="BUY", quantity=Decimal("1"),
            order_type="MARKET", risk_verdict="approved",
        )
    )
    ledger.record(
        PaperOrderRecord(
            key="2", decision_ref="2", asset="A", side="BUY", quantity=Decimal("1"),
            order_type="MARKET", risk_verdict="capped", risk_reason="size capped",
        )
    )
    ledger.record(
        PaperOrderRecord(
            key="3", decision_ref="3", asset="A", side="BUY", quantity=Decimal("1"),
            order_type="MARKET", risk_verdict="rejected", risk_reason="kill switch",
        )
    )
    service, _, _ = _service(ledger=ledger)
    stats = service.risk_intervention_stats()
    assert stats.decisions_evaluated == 3
    assert stats.approved == 1
    assert stats.capped == 1
    assert stats.rejected == 1
    assert stats.intervention_rate == pytest.approx(2 / 3)
    assert stats.reasons["size capped"] == 1
    assert stats.reasons["kill switch"] == 1
