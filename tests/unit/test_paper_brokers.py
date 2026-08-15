"""Unit tests for Phase 7 recording + shadow brokers."""

from __future__ import annotations

import pytest

from qtrader.application.paper.brokers import PaperExecutionBroker, ShadowBroker
from qtrader.application.paper.ledger import PaperOrderLedger
from qtrader.application.paper.models import PaperOrderStatus
from qtrader.application.paper.telemetry import OperationalTelemetry
from qtrader.domain.exceptions import NotFoundError
from tests.unit.fakes_paper import (
    FakeAgentMetricRepository,
    FakeBroker,
    FakePriceRepository,
    FakeRejectingBroker,
    FakeSystemLogRepository,
    make_order,
)


def _telemetry() -> tuple[OperationalTelemetry, FakeAgentMetricRepository, FakeSystemLogRepository]:
    metrics = FakeAgentMetricRepository()
    logs = FakeSystemLogRepository()
    return OperationalTelemetry(agent_metrics=metrics, logs=logs), metrics, logs


async def test_recording_broker_submit_records_submitted() -> None:
    ledger = PaperOrderLedger()
    telemetry, metrics, _ = _telemetry()
    inner = FakeBroker()
    broker = PaperExecutionBroker(inner=inner, ledger=ledger, telemetry=telemetry)

    bid = await broker.submit_order(make_order(limit_price="100.00"))

    assert bid == "fake-1"
    record = ledger.get_by_decision_ref("dec-1")
    assert record is not None
    assert record.status is PaperOrderStatus.SUBMITTED
    assert record.broker_order_id == "fake-1"
    assert record.requested_price is not None
    assert any(m.metric_name == "latency_ms_submit" for m in metrics.metrics)


async def test_recording_broker_submit_rejection_is_audited() -> None:
    ledger = PaperOrderLedger()
    telemetry, metrics, _ = _telemetry()
    broker = PaperExecutionBroker(
        inner=FakeRejectingBroker(RuntimeError("limit exceeded")),
        ledger=ledger,
        telemetry=telemetry,
    )

    with pytest.raises(RuntimeError, match="limit exceeded"):
        await broker.submit_order(make_order())

    record = ledger.get_by_decision_ref("dec-1")
    assert record is not None
    assert record.status is PaperOrderStatus.REJECTED
    assert record.rejection_reason == "limit exceeded"
    assert sum(m.value for m in metrics.metrics if m.metric_name == "api_failure") == 1


async def test_recording_broker_fill_updates_slippage_and_latency() -> None:
    ledger = PaperOrderLedger()
    telemetry, _, _ = _telemetry()
    inner = FakeBroker(fill_price="101.25")
    broker = PaperExecutionBroker(inner=inner, ledger=ledger, telemetry=telemetry)

    bid = await broker.submit_order(make_order(limit_price="100.00"))
    fill = await broker.get_order_status(bid)

    assert fill.status.value == "FILLED"
    record = ledger.get_by_decision_ref("dec-1")
    assert record is not None
    assert record.status is PaperOrderStatus.FILLED
    assert record.fill_price is not None and record.fill_price == inner.fill_price
    assert record.slippage is not None and record.slippage > 0
    assert record.execution_latency_ms is not None


async def test_recording_broker_cancel_marks_record_canceled() -> None:
    ledger = PaperOrderLedger()
    telemetry, _, _ = _telemetry()
    inner = FakeBroker()
    broker = PaperExecutionBroker(inner=inner, ledger=ledger, telemetry=telemetry)

    bid = await broker.submit_order(make_order())
    await broker.cancel_order(bid)

    assert bid in inner.canceled
    record = ledger.get_by_decision_ref("dec-1")
    assert record is not None
    assert record.status is PaperOrderStatus.CANCELED


async def test_shadow_broker_never_submits_and_records_shadow_only() -> None:
    ledger = PaperOrderLedger()
    telemetry, metrics, logs = _telemetry()
    broker = ShadowBroker(
        ledger=ledger,
        telemetry=telemetry,
        prices=FakePriceRepository({"AAPL": "150.00"}),
    )

    bid = await broker.submit_order(make_order(decision_ref="dec-shadow"))

    assert bid.startswith("shadow-")
    record = ledger.get_by_decision_ref("dec-shadow")
    assert record is not None
    assert record.status is PaperOrderStatus.SHADOW_ONLY
    assert record.shadow is True
    assert record.simulated_price is not None
    assert any(e.component == "paper.shadow" for e in logs.logs)
    assert len(metrics.metrics) == 0


async def test_shadow_broker_status_is_pending_and_never_fills() -> None:
    ledger = PaperOrderLedger()
    telemetry, _, _ = _telemetry()
    broker = ShadowBroker(ledger=ledger, telemetry=telemetry)

    bid = await broker.submit_order(make_order())
    fill = await broker.get_order_status(bid)

    assert fill.status.value == "PENDING"
    assert fill.filled_qty == 0


async def test_shadow_broker_unknown_order_raises() -> None:
    broker = ShadowBroker(ledger=PaperOrderLedger(), telemetry=OperationalTelemetry(
        agent_metrics=FakeAgentMetricRepository(), logs=FakeSystemLogRepository()
    ))
    with pytest.raises(NotFoundError):
        await broker.get_order_status("shadow-nope")
