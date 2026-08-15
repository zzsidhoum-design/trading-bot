"""Unit tests for the Phase 7 paper order ledger + stats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from qtrader.application.paper.ledger import PaperOrderLedger, ledger_stats
from qtrader.application.paper.models import (
    PaperOrderRecord,
    PaperOrderStatus,
)


def _record(
    key: str,
    *,
    status: PaperOrderStatus = PaperOrderStatus.FILLED,
    quantity: str = "10",
    fill_price: str | None = "101.25",
    requested_price: str | None = "100.00",
    slippage: str | None = "1.25",
    latency_ms: float | None = 40.0,
    risk_verdict: str | None = "approved",
    decision_ref: str | None = None,
    timestamp: datetime | None = None,
) -> PaperOrderRecord:
    return PaperOrderRecord(
        key=key,
        decision_ref=decision_ref,
        asset="AAPL",
        side="BUY",
        quantity=Decimal(quantity),
        order_type="MARKET",
        timestamp=timestamp or datetime.now(UTC),
        requested_price=(
            Decimal(requested_price) if requested_price is not None else None
        ),
        fill_price=Decimal(fill_price) if fill_price is not None else None,
        slippage=Decimal(slippage) if slippage is not None else None,
        status=status,
        execution_latency_ms=latency_ms,
        risk_verdict=risk_verdict,
    )


def test_ledger_round_trips_records() -> None:
    ledger = PaperOrderLedger()
    ledger.record(_record("a", decision_ref="dec-a"))
    ledger.record(_record("b", decision_ref="dec-b"))
    assert ledger.count() == 2
    assert ledger.get_by_decision_ref("dec-b") is not None
    assert ledger.get_by_decision_ref("dec-a").asset == "AAPL"
    assert ledger.get("missing") is None


def test_ledger_update_merges_and_preserves_risk_fields() -> None:
    ledger = PaperOrderLedger()
    ledger.record(_record("a", decision_ref="dec-a", risk_verdict="capped"))
    updated = ledger.update(
        "a",
        status=PaperOrderStatus.FILLED,
        fill_price=Decimal("101.00"),
        broker_order_id="paper-1",
    )
    assert updated.status is PaperOrderStatus.FILLED
    assert updated.risk_verdict == "capped"
    assert updated.broker_order_id == "paper-1"


def test_ledger_update_creates_stub_when_absent() -> None:
    ledger = PaperOrderLedger()
    record = ledger.update("new", status=PaperOrderStatus.SUBMITTED, asset="MSFT")
    assert record.status is PaperOrderStatus.SUBMITTED
    assert record.asset == "MSFT"
    assert ledger.count() == 1


def test_ledger_persistence_reload(tmp_path) -> None:
    path = tmp_path / "orders.jsonl"
    ledger = PaperOrderLedger(path)
    ledger.record(_record("a", decision_ref="dec-a"))
    ledger.record(_record("b", decision_ref="dec-b", status=PaperOrderStatus.REJECTED))
    assert ledger.write(path) == 2

    reloaded = PaperOrderLedger(path)
    assert reloaded.count() == 2
    assert reloaded.get_by_decision_ref("dec-a").quantity == Decimal("10")
    assert reloaded.get_by_decision_ref("dec-b").status is PaperOrderStatus.REJECTED


def test_ledger_stale_lists_only_submitted() -> None:
    ledger = PaperOrderLedger()
    ledger.record(_record("a", status=PaperOrderStatus.SUBMITTED))
    ledger.record(_record("b", status=PaperOrderStatus.FILLED))
    ledger.record(_record("c", status=PaperOrderStatus.PROPOSED))
    stale = ledger.stale()
    assert [r.key for r in stale] == ["a"]


def test_ledger_all_orders_by_timestamp(tmp_path) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = PaperOrderLedger(tmp_path / "o.jsonl")
    ledger.record(_record("a", timestamp=base + timedelta(days=2)))
    ledger.record(_record("b", timestamp=base))
    ledger.record(_record("c", timestamp=base + timedelta(days=1)))
    assert [r.key for r in ledger.all()] == ["b", "c", "a"]
    assert len(ledger.since(base + timedelta(days=1))) == 2


def test_stats_aggregates_fill_rate_slippage_and_risk() -> None:
    ledger = PaperOrderLedger()
    ledger.record(_record("a", status=PaperOrderStatus.FILLED, risk_verdict="approved"))
    ledger.record(_record("b", status=PaperOrderStatus.FILLED, risk_verdict="capped"))
    ledger.record(_record("c", status=PaperOrderStatus.REJECTED, risk_verdict="rejected"))
    ledger.record(_record("d", status=PaperOrderStatus.SUBMITTED, risk_verdict=None))
    ledger.record(_record("e", status=PaperOrderStatus.SHADOW_ONLY, risk_verdict=None))

    stats = ledger_stats(ledger)
    assert stats.total_orders == 5
    assert stats.filled == 2
    assert stats.rejected == 1
    assert stats.shadow_only == 1
    assert stats.fill_rate == 2 / 3
    assert stats.risk_approved == 1
    assert stats.risk_capped == 1
    assert stats.risk_rejected == 1
    assert stats.risk_not_gated == 2
    assert stats.avg_slippage_bps > 0
    assert stats.avg_execution_latency_ms == 40.0
