"""Unit tests for the Phase 7 acceptance evaluator (not profit-based)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from qtrader.application.paper.acceptance import (
    AcceptanceEvaluator,
    AcceptanceThresholds,
)
from qtrader.application.paper.comparison import (
    ComparisonInput,
    ComparisonReport,
    ComparisonRow,
    PaperVsResearchComparator,
)
from qtrader.application.paper.models import (
    PaperOrderRecord,
    PaperOrderStatus,
    PaperRunStats,
)
from qtrader.application.paper.telemetry import OperationalSummary


def _stats(**overrides) -> PaperRunStats:
    defaults = dict(
        total_orders=10,
        proposed=0,
        submitted=1,
        filled=10,
        partial=0,
        canceled=0,
        rejected=0,
        shadow_only=0,
        fill_rate=0.99,
        avg_slippage_bps=5.0,
        avg_execution_latency_ms=100.0,
        total_commission=Decimal("0"),
        risk_approved=9,
        risk_capped=1,
        risk_rejected=0,
        risk_not_gated=0,
        earliest=datetime(2026, 1, 1, tzinfo=UTC),
        latest=datetime(2026, 1, 2, tzinfo=UTC),
    )
    defaults.update(overrides)
    return PaperRunStats(**defaults)


def _operational(**overrides) -> OperationalSummary:
    defaults = dict(
        api_failures=1,
        missing_data=0,
        invalid_data=0,
        reconnections=0,
        latency_avg_ms={},
        signal_frequency={},
        data_events=100,
        data_reliability=1.0,
        failure_rate=0.0,
    )
    defaults.update(overrides)
    return OperationalSummary(**defaults)


def _comparison(
    total_return_divergence: float | None = 0.02,
    paper_drawdown: float | None = -0.05,
) -> ComparisonReport:
    return ComparisonReport(
        rows=(
            ComparisonRow(
                dimension="total_return",
                paper_value=0.05,
                research_value=0.03,
                divergence=total_return_divergence,
                interpretation="x",
            ),
            ComparisonRow(
                dimension="max_drawdown",
                paper_value=paper_drawdown,
                research_value=-0.04,
                divergence=None,
                interpretation="y",
            ),
        )
    )


def _evaluator(thresholds: AcceptanceThresholds | None = None) -> AcceptanceEvaluator:
    return AcceptanceEvaluator(thresholds or AcceptanceThresholds())


def test_acceptance_passes_for_healthy_run() -> None:
    result = _evaluator().evaluate(
        _stats(), _operational(), _comparison()
    )
    assert result.overall_passed is True
    names = [c.name for c in result.criteria]
    assert "fill_rate" in names
    assert "slippage" in names
    assert "execution_latency" in names
    assert "drawdown" in names
    assert "paper_research_divergence" in names
    assert "data_reliability" in names
    assert "failure_rate" in names


def test_acceptance_fails_on_low_fill_rate() -> None:
    result = _evaluator().evaluate(
        _stats(fill_rate=0.50), _operational(), _comparison()
    )
    assert result.overall_passed is False
    by_name = {c.name: c for c in result.criteria}
    assert by_name["fill_rate"].passed is False


def test_acceptance_fails_on_high_slippage() -> None:
    result = _evaluator().evaluate(
        _stats(avg_slippage_bps=120.0), _operational(), _comparison()
    )
    assert result.overall_passed is False
    assert {c.name: c for c in result.criteria}["slippage"].passed is False


def test_acceptance_fails_on_high_latency() -> None:
    result = _evaluator().evaluate(
        _stats(avg_execution_latency_ms=60_000.0), _operational(), _comparison()
    )
    assert result.overall_passed is False
    assert {c.name: c for c in result.criteria}["execution_latency"].passed is False


def test_acceptance_fails_on_drawdown_breach() -> None:
    result = _evaluator().evaluate(
        _stats(), _operational(), _comparison(paper_drawdown=-0.40)
    )
    assert result.overall_passed is False
    assert {c.name: c for c in result.criteria}["drawdown"].passed is False


def test_acceptance_fails_on_large_divergence() -> None:
    result = _evaluator().evaluate(
        _stats(), _operational(), _comparison(total_return_divergence=0.80)
    )
    assert result.overall_passed is False
    assert {c.name: c for c in result.criteria}["paper_research_divergence"].passed is False


def test_acceptance_fails_on_low_data_reliability() -> None:
    result = _evaluator().evaluate(
        _stats(), _operational(data_reliability=0.50), _comparison()
    )
    assert result.overall_passed is False
    assert {c.name: c for c in result.criteria}["data_reliability"].passed is False


def test_acceptance_fails_on_high_failure_rate() -> None:
    result = _evaluator().evaluate(
        _stats(), _operational(failure_rate=0.40), _comparison()
    )
    assert result.overall_passed is False
    assert {c.name: c for c in result.criteria}["failure_rate"].passed is False


def test_acceptance_is_not_profit_based() -> None:
    """A losing but operationally clean run still passes."""
    losing_comparison = PaperVsResearchComparator().compare(
        ComparisonInput(
            paper_records=(
                PaperOrderRecord(
                    key="1", asset="AAPL", side="BUY", quantity=Decimal("10"),
                    order_type="MARKET", fill_price=Decimal("100"),
                    slippage=Decimal("0"), status=PaperOrderStatus.FILLED,
                ),
            ),
            paper_trades=(),
            research_summary=None,
            research_signals=(),
        )
    )
    result = _evaluator().evaluate(_stats(), _operational(), losing_comparison)
    assert result.overall_passed is True
