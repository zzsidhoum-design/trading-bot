"""Phase 4 — execution simulation, slippage/liquidity/cost models, metrics."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from qtrader.application.execution.costs import TransactionCostModel
from qtrader.application.execution.liquidity import LiquidityModel
from qtrader.application.execution.metrics import (
    classify_execution,
    compute_execution_metrics,
    verdict_message,
)
from qtrader.application.execution.models import (
    ExecutionMetrics,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionScenario,
    ExecutionStats,
    ExecutionStatus,
    LiquidityAssessment,
    LiquidityAssumptions,
    SlippageAssumptions,
)
from qtrader.application.execution.simulator import ExecutionSimulator
from qtrader.application.execution.slippage import SlippageModel
from qtrader.domain.entities import PerformanceSummary
from qtrader.domain.value_objects import Interval, OrderType, PriceBar, TradeSide, TradingMode

UTC_ = UTC
EPS = Decimal("0.001")


def _bar(
    symbol: str,
    day: int,
    *,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal = Decimal("1000000"),
) -> PriceBar:
    return PriceBar(
        symbol=symbol,
        interval=Interval.D1,
        ts=datetime.combine(
            datetime(2024, 1, 1, tzinfo=UTC_).date() + timedelta(days=day),
            time(9, 30),
            tzinfo=UTC_,
        ),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _frictionless_slippage() -> SlippageModel:
    return SlippageModel(
        SlippageAssumptions(
            scenario=ExecutionScenario.OPTIMISTIC,
            base_spread_bps=0.0,
            base_slippage_bps=0.0,
            impact_coefficient=0.0,
            volatility_multiplier=0.0,
            latency_seconds=0.0,
            gap_threshold_pct=0.0,
            max_slippage_bps=100.0,
        )
    )


def _friction_slippage() -> SlippageModel:
    return SlippageModel(
        SlippageAssumptions(
            scenario=ExecutionScenario.BASELINE,
            base_spread_bps=1.0,
            base_slippage_bps=1.0,
            impact_coefficient=0.5,
            volatility_multiplier=1.0,
            latency_seconds=300.0,
            gap_threshold_pct=0.0,
            max_slippage_bps=100.0,
        )
    )


def _loose_liquidity() -> LiquidityModel:
    return LiquidityModel(
        LiquidityAssumptions(
            min_avg_volume=Decimal("0"),
            min_avg_dollar_volume=Decimal("0"),
            max_notional_pct_adv=1.0,
            max_participation_rate=0.10,
        )
    )


def _simulator(
    slippage: SlippageModel | None = None,
    liquidity: LiquidityModel | None = None,
) -> ExecutionSimulator:
    return ExecutionSimulator(
        slippage or _frictionless_slippage(),
        liquidity or _loose_liquidity(),
        TransactionCostModel(commission_bps=0.0, min_commission=Decimal("0")),
        seed=42,
    )


def _step(sim: ExecutionSimulator, bar: PriceBar) -> list:
    """Process a bar with generous ADV so orders pass the liquidity gate."""
    return sim.process_bar(
        bar,
        adv_volume=Decimal("1000000"),
        adv_dollar=Decimal("100000000"),
    )


def _summary(*, total_return: str, sharpe: str) -> PerformanceSummary:
    return PerformanceSummary(
        strategy="t",
        mode=TradingMode.BACKTEST,
        period_start=datetime(2024, 1, 1, tzinfo=UTC_).date(),
        period_end=datetime(2024, 1, 31, tzinfo=UTC_).date(),
        total_return=Decimal(total_return),
        sharpe=Decimal(sharpe),
    )


# --------------------------------------------------------------------------- #
# SlippageModel
# --------------------------------------------------------------------------- #


class TestSlippageModel:
    def test_buy_fills_worse_than_reference(self) -> None:
        model = _friction_slippage()
        price, bps = model.fill_price(
            side=TradeSide.BUY,
            reference_price=Decimal("100"),
            order_notional=Decimal("10000"),
            adv_dollar=Decimal("1000000"),
            atr_pct=0.0,
        )
        assert price > Decimal("100")
        assert bps > 0

    def test_sell_fills_worse_than_reference(self) -> None:
        model = _friction_slippage()
        price, bps = model.fill_price(
            side=TradeSide.SELL,
            reference_price=Decimal("100"),
            order_notional=Decimal("10000"),
            adv_dollar=Decimal("1000000"),
            atr_pct=0.0,
        )
        assert price < Decimal("100")
        assert bps > 0

    def test_impact_grows_with_participation(self) -> None:
        model = _friction_slippage()
        small = model.slippage_bps(
            order_notional=Decimal("10000"),
            adv_dollar=Decimal("1000000"),
            atr_pct=0.0,
        )
        large = model.slippage_bps(
            order_notional=Decimal("100000"),
            adv_dollar=Decimal("1000000"),
            atr_pct=0.0,
        )
        assert large > small

    def test_slippage_capped_at_max(self) -> None:
        model = SlippageModel(
            SlippageAssumptions(
                scenario=ExecutionScenario.OPTIMISTIC,
                base_spread_bps=0.0,
                base_slippage_bps=0.0,
                impact_coefficient=1.0,
                volatility_multiplier=0.0,
                latency_seconds=0.0,
                gap_threshold_pct=0.0,
                max_slippage_bps=50.0,
            )
        )
        bps = model.slippage_bps(
            order_notional=Decimal("10000000"),
            adv_dollar=Decimal("1000000"),
            atr_pct=0.0,
        )
        assert bps == 50.0

    def test_no_impact_without_adv(self) -> None:
        model = _friction_slippage()
        bps = model.slippage_bps(
            order_notional=Decimal("10000"),
            adv_dollar=None,
            atr_pct=0.0,
        )
        assert bps >= 0

    def test_adverse_drift_grows_with_atr(self) -> None:
        model = _friction_slippage()
        low = model.slippage_bps(
            order_notional=Decimal("1000"),
            adv_dollar=Decimal("1000000"),
            atr_pct=0.005,
        )
        high = model.slippage_bps(
            order_notional=Decimal("1000"),
            adv_dollar=Decimal("1000000"),
            atr_pct=0.05,
        )
        assert high > low


# --------------------------------------------------------------------------- #
# LiquidityModel
# --------------------------------------------------------------------------- #


class TestLiquidityModel:
    def test_adv_averages_window(self) -> None:
        model = _loose_liquidity()
        bars = [
            _bar("A", i, open_=Decimal("100"), high=Decimal("101"),
                 low=Decimal("99"), close=Decimal("100"), volume=Decimal("2000"))
            for i in range(3)
        ]
        adv_volume, adv_dollar = model.adv_for(bars)
        assert adv_volume == Decimal("2000")
        assert adv_dollar == Decimal("200000")

    def test_check_size_rejects_below_volume_floor(self) -> None:
        model = LiquidityModel(
            LiquidityAssumptions(
                min_avg_volume=Decimal("50000"),
                min_avg_dollar_volume=Decimal("500000"),
                max_notional_pct_adv=0.01,
            )
        )
        assessment = model.check_size(
            order_notional=Decimal("1000"),
            adv_volume=Decimal("10000"),
            adv_dollar=Decimal("1000000"),
        )
        assert not assessment.approved
        assert any("below floor" in r for r in assessment.reasons)

    def test_check_size_rejects_unrealistic_size(self) -> None:
        model = LiquidityModel(
            LiquidityAssumptions(
                min_avg_volume=Decimal("0"),
                min_avg_dollar_volume=Decimal("0"),
                max_notional_pct_adv=0.01,
            )
        )
        assessment = model.check_size(
            order_notional=Decimal("20000"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("1000000"),
        )
        assert not assessment.approved
        assert any("unrealistic trade size" in r for r in assessment.reasons)

    def test_check_size_approves_small_order(self) -> None:
        model = _loose_liquidity()
        assessment = model.check_size(
            order_notional=Decimal("1000"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("100000000"),
        )
        assert assessment.approved

    def test_max_fillable_applies_participation_cap(self) -> None:
        model = _loose_liquidity()
        bar = _bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                   low=Decimal("99"), close=Decimal("100"), volume=Decimal("10000"))
        assert model.max_fillable(bar) == 1000


# --------------------------------------------------------------------------- #
# TransactionCostModel
# --------------------------------------------------------------------------- #


class TestTransactionCostModel:
    def test_commission_scales_with_notional(self) -> None:
        model = TransactionCostModel(commission_bps=10.0)
        small = model.commission_for(100, Decimal("100"))
        large = model.commission_for(1000, Decimal("100"))
        assert small == Decimal("10.00")
        assert large == Decimal("100.00")

    def test_min_commission_floor(self) -> None:
        model = TransactionCostModel(commission_bps=10.0, min_commission=Decimal("1.00"))
        assert model.commission_for(1, Decimal("100")) == Decimal("1.00")

    def test_zero_commission(self) -> None:
        model = TransactionCostModel(commission_bps=0.0)
        assert model.commission_for(1000, Decimal("100")) == Decimal("0.00")


# --------------------------------------------------------------------------- #
# ExecutionSimulator
# --------------------------------------------------------------------------- #


class TestExecutionSimulator:
    def test_market_order_fills_at_next_bar_open(self) -> None:
        sim = _simulator()
        assert sim.submit(
            ExecutionOrder(
                symbol="A",
                side=TradeSide.BUY,
                quantity=1000,
                order_type=OrderType.MARKET,
                signal_ts=_bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                               low=Decimal("99"), close=Decimal("100")).ts,
            ),
            ref_price=Decimal("100"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("100000000"),
        )
        fills = _step(
            sim,
            _bar("A", 1, open_=Decimal("100"), high=Decimal("101"),
                 low=Decimal("99"), close=Decimal("100"))
        )
        assert len(fills) == 1
        assert fills[0].quantity == 1000
        assert fills[0].price == Decimal("100")
        assert sim.stats.filled == 1

    def test_sell_market_fills_at_open(self) -> None:
        sim = _simulator()
        sim.submit(
            ExecutionOrder(
                symbol="A",
                side=TradeSide.SELL,
                quantity=500,
                order_type=OrderType.MARKET,
                signal_ts=_bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                               low=Decimal("99"), close=Decimal("100")).ts,
            ),
            ref_price=Decimal("100"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("100000000"),
        )
        fills = _step(
            sim,
            _bar("A", 1, open_=Decimal("100"), high=Decimal("101"),
                 low=Decimal("99"), close=Decimal("100"))
        )
        assert fills[0].side is TradeSide.SELL
        assert fills[0].quantity == 500

    def test_order_is_rejected_for_unrealistic_size(self) -> None:
        sim = _simulator(liquidity=LiquidityModel(LiquidityAssumptions(
            min_avg_volume=Decimal("0"),
            min_avg_dollar_volume=Decimal("0"),
            max_notional_pct_adv=0.01,
        )))
        accepted = sim.submit(
            ExecutionOrder(
                symbol="A",
                side=TradeSide.BUY,
                quantity=100000,
                order_type=OrderType.MARKET,
                signal_ts=_bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                               low=Decimal("99"), close=Decimal("100")).ts,
            ),
            ref_price=Decimal("100"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("1000000"),
        )
        assert not accepted
        assert sim.stats.rejected == 1
        assert sim.stats.unrealistic_orders == 1

    def test_partial_fill_completes_on_later_bar(self) -> None:
        sim = _simulator()
        thin = _bar("A", 1, open_=Decimal("100"), high=Decimal("101"),
                    low=Decimal("99"), close=Decimal("100"), volume=Decimal("1000"))
        sim.submit(
            ExecutionOrder(
                symbol="A",
                side=TradeSide.BUY,
                quantity=150,
                order_type=OrderType.MARKET,
                signal_ts=_bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                               low=Decimal("99"), close=Decimal("100")).ts,
            ),
            ref_price=Decimal("100"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("100000000"),
        )
        first = _step(sim, thin)
        assert len(first) == 1
        assert first[0].quantity == 100
        assert first[0].partial
        second = _step(
            sim,
            _bar("A", 2, open_=Decimal("100"), high=Decimal("101"),
                 low=Decimal("99"), close=Decimal("100"), volume=Decimal("100000"))
        )
        assert len(second) == 1
        assert second[0].quantity == 50
        assert sim.stats.filled == 1
        assert sim.stats.partial_fills == 1

    def test_buy_stop_gap_through_fills_at_open(self) -> None:
        sim = _simulator()
        sim.submit(
            ExecutionOrder(
                symbol="A",
                side=TradeSide.BUY,
                quantity=100,
                order_type=OrderType.STOP,
                stop_price=Decimal("100"),
                signal_ts=_bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                               low=Decimal("99"), close=Decimal("100")).ts,
            ),
            ref_price=Decimal("100"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("100000000"),
        )
        fills = _step(
            sim,
            _bar("A", 1, open_=Decimal("105"), high=Decimal("106"),
                 low=Decimal("104"), close=Decimal("105"))
        )
        assert len(fills) == 1
        assert fills[0].price == Decimal("105")

    def test_sell_stop_untriggered_stays_working(self) -> None:
        sim = _simulator()
        sim.submit(
            ExecutionOrder(
                symbol="A",
                side=TradeSide.SELL,
                quantity=100,
                order_type=OrderType.STOP,
                stop_price=Decimal("95"),
                signal_ts=_bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                               low=Decimal("99"), close=Decimal("100")).ts,
            ),
            ref_price=Decimal("100"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("100000000"),
        )
        fills = _step(
            sim,
            _bar("A", 1, open_=Decimal("100"), high=Decimal("101"),
                 low=Decimal("99"), close=Decimal("100"))
        )
        assert fills == []
        assert len(sim.pending) == 1

    def test_limit_fills_only_when_price_trades_through(self) -> None:
        sim = _simulator()
        sim.submit(
            ExecutionOrder(
                symbol="A",
                side=TradeSide.BUY,
                quantity=100,
                order_type=OrderType.LIMIT,
                limit_price=Decimal("95"),
                signal_ts=_bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                               low=Decimal("99"), close=Decimal("100")).ts,
            ),
            ref_price=Decimal("100"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("100000000"),
        )
        no_fill = _step(
            sim,
            _bar("A", 1, open_=Decimal("100"), high=Decimal("101"),
                 low=Decimal("96"), close=Decimal("100"))
        )
        assert no_fill == []
        fills = _step(
            sim,
            _bar("A", 2, open_=Decimal("97"), high=Decimal("98"),
                 low=Decimal("94"), close=Decimal("95"))
        )
        assert len(fills) == 1
        assert fills[0].price == Decimal("95")

    def test_new_order_replaces_same_side(self) -> None:
        sim = _simulator()
        ts = _bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                  low=Decimal("99"), close=Decimal("100")).ts
        for qty in (1000, 500):
            sim.submit(
                ExecutionOrder(
                    symbol="A",
                    side=TradeSide.BUY,
                    quantity=qty,
                    order_type=OrderType.MARKET,
                    signal_ts=ts,
                ),
                ref_price=Decimal("100"),
                adv_volume=Decimal("1000000"),
                adv_dollar=Decimal("100000000"),
            )
        assert len(sim.pending) == 1
        assert sim.pending[0].quantity == 500

    def test_cancel_side_removes_working_orders(self) -> None:
        sim = _simulator()
        sim.submit(
            ExecutionOrder(
                symbol="A",
                side=TradeSide.BUY,
                quantity=1000,
                order_type=OrderType.MARKET,
                signal_ts=_bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                               low=Decimal("99"), close=Decimal("100")).ts,
            ),
            ref_price=Decimal("100"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("100000000"),
        )
        sim.cancel_side("A", TradeSide.BUY)
        assert sim.pending == []
        assert sim.stats.canceled == 1

    def test_stats_record_slippage_and_deviation(self) -> None:
        sim = _simulator()
        sim.submit(
            ExecutionOrder(
                symbol="A",
                side=TradeSide.BUY,
                quantity=100,
                order_type=OrderType.MARKET,
                signal_ts=_bar("A", 0, open_=Decimal("100"), high=Decimal("101"),
                               low=Decimal("99"), close=Decimal("100")).ts,
            ),
            ref_price=Decimal("100"),
            adv_volume=Decimal("1000000"),
            adv_dollar=Decimal("100000000"),
        )
        _step(
            sim,
            _bar("A", 1, open_=Decimal("100"), high=Decimal("101"),
                 low=Decimal("99"), close=Decimal("100"))
        )
        assert sim.stats.slippage_bps_values == [0.0]
        assert sim.stats.deviation_bps_values == [0.0]


# --------------------------------------------------------------------------- #
# compute_execution_metrics
# --------------------------------------------------------------------------- #


def _metrics_base() -> ExecutionMetrics:
    return ExecutionMetrics(
        scenario=ExecutionScenario.BASELINE,
        expected_slippage_bps=5.0,
        avg_execution_deviation_bps=2.0,
        fill_rate=1.0,
        partial_fill_rate=0.0,
        rejected_rate=0.0,
        transaction_costs=Decimal("10.00"),
        turnover=2.0,
        net_return=0.1,
        net_sharpe=1.0,
        net_sortino=1.5,
        max_drawdown=-0.1,
        trades=5,
        degradation_return=0.02,
        degradation_sharpe=0.1,
        liquidity_flags=(),
    )


class TestComputeExecutionMetrics:
    def test_rates_and_degradation(self) -> None:
        stats = ExecutionStats(submitted=4, filled=3, partial_fills=1, rejected=1)
        metrics = compute_execution_metrics(
            scenario=ExecutionScenario.BASELINE,
            theoretical=_summary(total_return="0.15", sharpe="1.5"),
            execution_summary=_summary(total_return="0.10", sharpe="1.2"),
            execution_equity_curve=[
                (datetime(2024, 1, 2, tzinfo=UTC_), Decimal("100000")),
                (datetime(2024, 1, 3, tzinfo=UTC_), Decimal("101000")),
            ],
            trades=[],
            stats=stats,
            assessments={},
            adv_seen={},
            liquidity=LiquidityAssumptions(),
        )
        assert metrics.fill_rate == pytest.approx(0.75)
        assert metrics.rejected_rate == pytest.approx(0.25)
        assert metrics.partial_fill_rate == pytest.approx(1 / 3)
        assert metrics.degradation_return == pytest.approx(0.05)
        assert metrics.degradation_sharpe == pytest.approx(0.3)
        assert metrics.turnover == 0.0

    def test_liquidity_flags(self) -> None:
        metrics = compute_execution_metrics(
            scenario=ExecutionScenario.BASELINE,
            theoretical=_summary(total_return="0.10", sharpe="1.0"),
            execution_summary=_summary(total_return="0.08", sharpe="0.9"),
            execution_equity_curve=[],
            trades=[],
            stats=ExecutionStats(),
            assessments={
                "A": LiquidityAssessment(approved=False, reasons=("thin",), max_fillable=0)
            },
            adv_seen={"A": (Decimal("1000"), Decimal("100000"))},
            liquidity=LiquidityAssumptions(
                min_avg_volume=Decimal("50000"),
                min_avg_dollar_volume=Decimal("500000"),
            ),
        )
        flags = set(metrics.liquidity_flags)
        assert "A:below-min-avg-volume" in flags
        assert "A:below-min-avg-dollar-volume" in flags
        assert "A:thin" in flags

    def test_rejection_messages(self) -> None:
        metrics = compute_execution_metrics(
            scenario=ExecutionScenario.BASELINE,
            theoretical=_summary(total_return="0.10", sharpe="1.0"),
            execution_summary=_summary(total_return="0.08", sharpe="0.9"),
            execution_equity_curve=[],
            trades=[],
            stats=ExecutionStats(),
            assessments={
                "A": LiquidityAssessment(approved=False, reasons=("thin",), max_fillable=0)
            },
            adv_seen={"A": (Decimal("1000"), Decimal("100000"))},
            liquidity=LiquidityAssumptions(
                min_avg_volume=Decimal("50000"),
                min_avg_dollar_volume=Decimal("500000"),
            ),
        )
        messages = set(metrics.rejection_messages)
        assert "REJECTED: A average daily volume 1,000 shares below floor 50,000" in messages
        assert (
            "REJECTED: A average daily dollar volume $100,000 below floor $500,000"
            in messages
        )
        assert "REJECTED: A thin" in messages


# --------------------------------------------------------------------------- #
# classify_execution
# --------------------------------------------------------------------------- #


class TestClassifyExecution:
    def test_robust(self) -> None:
        status = classify_execution(
            baseline=_metrics_base(),
            worst_degradation_sharpe=0.2,
            worst_degradation_return=0.1,
            plan=ExecutionPlan(),
        )
        assert status is ExecutionStatus.EXECUTION_ROBUST

    def test_rejected_on_low_fill_rate(self) -> None:
        baseline = replace(_metrics_base(), fill_rate=0.5)
        status = classify_execution(
            baseline=baseline,
            worst_degradation_sharpe=0.0,
            worst_degradation_return=0.0,
            plan=ExecutionPlan(),
        )
        assert status is ExecutionStatus.EXECUTION_REJECTED

    def test_rejected_on_negative_net_sharpe(self) -> None:
        baseline = replace(_metrics_base(), net_sharpe=-0.5)
        status = classify_execution(
            baseline=baseline,
            worst_degradation_sharpe=0.0,
            worst_degradation_return=0.0,
            plan=ExecutionPlan(),
        )
        assert status is ExecutionStatus.EXECUTION_REJECTED

    def test_sensitive_on_sharpe_degradation(self) -> None:
        status = classify_execution(
            baseline=_metrics_base(),
            worst_degradation_sharpe=2.0,
            worst_degradation_return=0.0,
            plan=ExecutionPlan(),
        )
        assert status is ExecutionStatus.EXECUTION_SENSITIVE

    def test_sensitive_on_return_degradation(self) -> None:
        status = classify_execution(
            baseline=_metrics_base(),
            worst_degradation_sharpe=0.0,
            worst_degradation_return=0.9,
            plan=ExecutionPlan(),
        )
        assert status is ExecutionStatus.EXECUTION_SENSITIVE


# --------------------------------------------------------------------------- #
# verdict_message
# --------------------------------------------------------------------------- #


class TestVerdictMessage:
    def test_robust(self) -> None:
        message = verdict_message(
            status=ExecutionStatus.EXECUTION_ROBUST,
            baseline=_metrics_base(),
            worst_degradation_sharpe=0.1,
            worst_degradation_return=0.1,
            plan=ExecutionPlan(),
        )
        assert message.startswith("EXECUTION ROBUST:")

    def test_rejected_reasons_are_human_readable(self) -> None:
        baseline = replace(_metrics_base(), fill_rate=0.5)
        message = verdict_message(
            status=ExecutionStatus.EXECUTION_REJECTED,
            baseline=baseline,
            worst_degradation_sharpe=0.0,
            worst_degradation_return=0.0,
            plan=ExecutionPlan(),
        )
        assert "REJECTED: fill rate" in message

    def test_sensitive_reason(self) -> None:
        message = verdict_message(
            status=ExecutionStatus.EXECUTION_SENSITIVE,
            baseline=_metrics_base(),
            worst_degradation_sharpe=2.0,
            worst_degradation_return=0.0,
            plan=ExecutionPlan(),
        )
        assert "SENSITIVE:" in message
        assert "Sharpe degradation 2.00" in message

    def test_carries_liquidity_rejection_messages(self) -> None:
        baseline = replace(
            _metrics_base(),
            rejection_messages=("REJECTED: average daily volume below 50,000 shares",),
        )
        message = verdict_message(
            status=ExecutionStatus.EXECUTION_REJECTED,
            baseline=baseline,
            worst_degradation_sharpe=0.0,
            worst_degradation_return=0.0,
            plan=ExecutionPlan(),
        )
        assert "REJECTED: average daily volume below 50,000 shares" in message
