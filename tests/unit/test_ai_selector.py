"""Phase 6 — Multi-factor strategy selector (never returns-only)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from qtrader.application.ai.regime import MarketRegimeAgent
from qtrader.application.ai.selector import StrategySelector
from qtrader.application.research.strategy.specs import (
    Condition,
    EntryRule,
    Operator,
    RegimeFilter,
    StrategySpec,
)
from qtrader.application.research.validation.records import FinalStatus
from tests.unit.fakes_ai import rising_closes
from tests.unit.fakes_portfolio_mgmt import make_validation_record


def _spec(**overrides: object) -> StrategySpec:
    params: dict[str, object] = dict(
        id="s1",
        name="s1",
        entry=EntryRule(
            conditions=(Condition(feature="close", op=Operator.GT, value=50.0),)
        ),
    )
    params.update(overrides)
    return StrategySpec(**params)


def _regime_condition() -> RegimeFilter:
    return RegimeFilter(
        conditions=(Condition(feature="sma_fast", op=Operator.GT, ref_feature="sma_slow"),)
    )


def test_ranks_high_sharpe_first() -> None:
    selector = StrategySelector()
    strong = make_validation_record("a", sharpe=Decimal("2.5"), total_return=Decimal("0.5"))
    weak = make_validation_record("b", sharpe=Decimal("0.3"), total_return=Decimal("0.02"))
    report = selector.select([weak, strong])
    assert [s.strategy_id for s in report.selections] == ["a", "b"]
    assert report.selections[0].score >= report.selections[1].score


def test_excludes_non_eligible_statuses() -> None:
    selector = StrategySelector()
    failed = make_validation_record(
        "a", final_status=FinalStatus.REJECTED, stage=FinalStatus.REJECTED
    )
    report = selector.select([failed])
    assert report.selections == ()
    assert report.excluded[0].strategy_id == "a"
    assert "status:" in report.excluded[0].reason


def test_excludes_suspended_strategies() -> None:
    selector = StrategySelector()
    record = make_validation_record("a")
    report = selector.select([record], suspended={"a"})
    assert report.selections == ()
    assert report.excluded[0].reason == "suspended"


def test_excludes_unverifiable_regime_conditions() -> None:
    selector = StrategySelector()
    record = make_validation_record("a")
    bad_spec = _spec(
        regime=RegimeFilter(
            conditions=(
                Condition(feature="sma_fast", op=Operator.CROSS_ABOVE, ref_feature="sma_slow"),
            )
        )
    )
    record = replace(record, spec=bad_spec)
    report = selector.select([record], features={"sma_fast": 1.0, "sma_slow": 0.5})
    assert report.selections == ()
    assert report.excluded[0].reason == "regime_cross_condition_unverifiable"


def test_excludes_strategy_with_violated_regime_condition() -> None:
    selector = StrategySelector()
    record = make_validation_record("a")
    bad_spec = _spec(regime=_regime_condition())
    record = replace(record, spec=bad_spec)
    report = selector.select([record], features={"sma_fast": 0.1, "sma_slow": 0.9})
    assert report.selections == ()
    assert report.excluded[0].reason == "regime_condition_violated"


def test_passes_when_regime_condition_met() -> None:
    selector = StrategySelector()
    record = make_validation_record("a")
    good_spec = _spec(regime=_regime_condition())
    record = replace(record, spec=good_spec)
    report = selector.select([record], features={"sma_fast": 0.9, "sma_slow": 0.1})
    assert len(report.selections) == 1


def test_excludes_missing_oos() -> None:
    selector = StrategySelector()
    record = make_validation_record("a")
    record = replace(record, oos_result=None)
    report = selector.select([record])
    assert report.selections == ()
    assert report.excluded[0].reason == "missing_oos"


def test_never_raises_on_bad_records() -> None:
    selector = StrategySelector()
    record = make_validation_record("a", sharpe=Decimal("-5"))
    report = selector.select([record])
    assert isinstance(report, object)


def test_regime_suitability_reflects_volatility_match() -> None:
    agent = MarketRegimeAgent()
    closes = [
        (datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=i), c)
        for i, c in enumerate(rising_closes(300))
    ]
    regime = agent.assess(closes)
    assert regime is not None
    record = make_validation_record("a")
    report = StrategySelector().select([record], regime=regime)
    sel = report.selections[0]
    assert 0.0 <= sel.regime_suitability <= 1.0


def test_report_as_of_and_regime_are_exposed() -> None:
    selector = StrategySelector()
    record = make_validation_record("a")
    now = datetime(2025, 1, 1, tzinfo=UTC)
    report = selector.select([record], as_of=now)
    assert report.as_of == now
    assert report.regime is None
