"""Phase 2 — automated strategy research engine tests."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from qtrader.application.research.strategy import (
    FEATURES,
    Condition,
    EntryRule,
    ExitRule,
    FeatureCategory,
    FeatureLibrary,
    GenerationResult,
    InMemoryStrategyRegistry,
    Operator,
    RegimeFilter,
    ResearchPlan,
    ResearchReport,
    ResearchRequest,
    RobustnessChecker,
    RobustnessLimits,
    SearchLimits,
    StrategyEvaluator,
    StrategyGenerator,
    StrategyRecord,
    StrategySpec,
    StrategyStatus,
    StrategyWalkForwardValidator,
    decode_spec,
    encode_spec,
)
from qtrader.application.research.strategy.engine import MetricGate, StrategyResearchEngine
from qtrader.application.services.indicators import IndicatorEngine
from qtrader.application.services.risk_calculator import RiskCalculator, RiskPolicy
from qtrader.domain.entities import PerformanceSummary
from qtrader.domain.ports import PriceRepository
from qtrader.domain.value_objects import Interval, PriceBar, TradingMode
from tests.unit.fakes_phase7 import FakePerformanceRepository

_UTC = UTC
_EPS = Decimal("0.01")


def _make_bars(symbol: str, start: date, n: int, seed: int, drift: float) -> list[PriceBar]:
    rng = random.Random(seed)
    price = 100.0
    bars: list[PriceBar] = []
    ts = datetime.combine(start, time(9, 30), tzinfo=_UTC)
    for _ in range(n):
        ret = drift + rng.gauss(0.0, 0.012)
        open_ = price
        close = price * (1 + ret)
        high = max(open_, close) * (1 + abs(rng.gauss(0.0, 0.004)))
        low = min(open_, close) * (1 - abs(rng.gauss(0.0, 0.004)))
        volume = 1_000_000 * (0.8 + rng.random() * 0.4)
        bars.append(
            PriceBar(
                symbol=symbol,
                interval=Interval.D1,
                ts=ts,
                open=_round(open_),
                high=_round(high),
                low=_round(low),
                close=_round(close),
                volume=_round(volume),
            )
        )
        price = close
        ts += timedelta(days=1)
    return bars


def _round(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))


class _BarsPriceRepository(PriceRepository):
    def __init__(self, bars_by_symbol: dict[str, list[PriceBar]]) -> None:
        self._bars = {s: sorted(b, key=lambda b: b.ts) for s, b in bars_by_symbol.items()}

    async def upsert_bars(self, bars: list[PriceBar]) -> int:
        return len(bars)

    async def latest(self, symbol: str, interval: Interval) -> PriceBar | None:
        bars = self._bars.get(symbol, [])
        return bars[-1] if bars else None

    async def history(
        self,
        symbol: str,
        interval: Interval,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[PriceBar]:
        bars = [
            b
            for b in self._bars.get(symbol, [])
            if (start is None or b.ts >= start) and (end is None or b.ts <= end)
        ]
        return bars[:limit]


def _default_spec(**overrides: object) -> StrategySpec:
    spec = StrategySpec(
        id="strat-0001",
        name="auto-0001",
        entry=EntryRule(
            conditions=(Condition(feature="close", op=Operator.GT, ref_feature="ema_21"),)
        ),
        exit=ExitRule(conditions=(Condition(feature="rsi", op=Operator.GT, value=70.0),)),
        features=("close", "ema_21", "rsi"),
        complexity=3,
        description="trend entry, rsi exit",
    )
    return replace(spec, **overrides)


# --------------------------------------------------------------------------- #
# Feature library
# --------------------------------------------------------------------------- #


class TestFeatureLibrary:
    def test_categories_are_disjoint_and_complete(self) -> None:
        library = FeatureLibrary()
        names = library.names()
        assert len(names) == len(set(names))
        for category in library.categories():
            for feature in library.by_category(category):
                assert feature.category is category
        assert FeatureCategory.TREND in library.categories()

    def test_registry_has_required_families(self) -> None:
        by_name = {f.name: f for f in FEATURES}
        for required in ("rsi", "ema_21", "sma_50", "macd_hist", "atr", "volume_ratio", "adx"):
            assert required in by_name


# --------------------------------------------------------------------------- #
# Specs
# --------------------------------------------------------------------------- #


class TestSpecs:
    def test_encode_decode_roundtrip(self) -> None:
        regime = RegimeFilter(
            conditions=(Condition(feature="close", op=Operator.GT, ref_feature="sma_200"),)
        )
        spec = _default_spec(regime=regime)
        restored = decode_spec(encode_spec(spec))
        assert restored == spec
        assert restored.timeframes == (Interval.D1,)

    def test_spec_requires_rules(self) -> None:
        with pytest.raises(ValueError):
            StrategySpec(id="x", name="x", entry=EntryRule(), exit=ExitRule())

    def test_spec_rejects_unknown_direction(self) -> None:
        with pytest.raises(ValueError):
            _default_spec(direction="sideways")


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


class TestGenerator:
    def test_respects_max_strategies_cap(self) -> None:
        generator = StrategyGenerator()
        result = generator.generate(SearchLimits(max_strategies=10))
        assert len(result.specs) == 10
        ids = [s.id for s in result.specs]
        assert len(set(ids)) == len(ids)

    def test_specs_are_well_formed(self) -> None:
        generator = StrategyGenerator()
        result = generator.generate(SearchLimits(max_strategies=12))
        for spec in result.specs:
            assert spec.version == 1
            assert spec.direction == "long"
            assert spec.entry.conditions
            assert spec.exit.conditions
            assert spec.timeframes
            assert spec.complexity >= 1

    def test_constraints_reject_excessive_conditions(self) -> None:
        generator = StrategyGenerator()
        result = generator.generate(SearchLimits(max_strategies=60, max_conditions=1))
        assert result.rejected_count > 0
        assert any("too many entry conditions" in reason for reason in result.rejections.values())

    def test_budget_defaults_generate_full_pool(self) -> None:
        result = StrategyGenerator().generate(SearchLimits())
        assert result.candidates_considered == 60
        assert 0 < len(result.specs) <= 60
        assert isinstance(result, GenerationResult)


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #


class TestEvaluator:
    def _run(self, spec: StrategySpec, bars: list[PriceBar]) -> dict[datetime, float]:
        series = IndicatorEngine().compute_series(bars, bars[0].symbol, Interval.D1)
        return StrategyEvaluator(warmup_bars=30).probs(
            spec, {bars[0].symbol: bars}, {bars[0].symbol: series}
        )[bars[0].symbol]

    def test_warmup_bars_hold(self) -> None:
        bars = _make_bars("AAA", date(2020, 1, 1), 120, seed=3, drift=0.0)
        probs = self._run(_default_spec(), bars)
        assert all(p == 0.5 for p in list(probs.values())[:30])

    def test_entry_and_exit_fire(self) -> None:
        bars = _make_bars("AAA", date(2020, 1, 1), 200, seed=3, drift=0.001)
        probs = self._run(_default_spec(), bars)
        values = list(probs.values())
        assert any(v == 0.9 for v in values)
        assert any(v == 0.1 for v in values)

    def test_regime_gate_suppresses_entries(self) -> None:
        bars = _make_bars("AAA", date(2020, 1, 1), 200, seed=4, drift=-0.002)
        spec = StrategySpec(
            id="strat-0002",
            name="auto-0002",
            entry=EntryRule(
                conditions=(Condition(feature="close", op=Operator.GT, ref_feature="ema_21"),)
            ),
            exit=ExitRule(conditions=(Condition(feature="rsi", op=Operator.LT, value=40.0),)),
            regime=RegimeFilter(
                conditions=(Condition(feature="close", op=Operator.GT, ref_feature="sma_200"),)
            ),
        )
        probs = self._run(spec, bars)
        # In a falling market close never exceeds its 200-day SMA -> no buys.
        assert not any(v == 0.9 for v in probs.values())

    def test_cross_operator(self) -> None:
        bars = _make_bars("AAA", date(2020, 1, 1), 200, seed=5, drift=0.001)
        spec = StrategySpec(
            id="strat-0003",
            name="auto-0003",
            entry=EntryRule(
                conditions=(Condition(feature="rsi", op=Operator.CROSS_ABOVE, value=50.0),)
            ),
            exit=ExitRule(
                conditions=(Condition(feature="rsi", op=Operator.CROSS_BELOW, value=60.0),)
            ),
        )
        probs = self._run(spec, bars)
        assert any(v == 0.9 for v in probs.values())

    def test_missing_series_is_hold(self) -> None:
        bars = _make_bars("AAA", date(2020, 1, 1), 120, seed=6, drift=0.0)
        probs = StrategyEvaluator().probs(_default_spec(), {"AAA": bars}, {})["AAA"]
        assert all(p == 0.5 for p in probs.values())


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_crud_and_status_flow(self) -> None:
        registry = InMemoryStrategyRegistry()
        spec = _default_spec()
        record = registry.register(spec)
        assert registry.get(spec.id) is not None
        assert record.status is StrategyStatus.GENERATED

        registry.set_status(spec.id, StrategyStatus.VALIDATED, note="ok")
        updated = registry.get(spec.id)
        assert updated is not None and updated.status is StrategyStatus.VALIDATED
        assert updated.notes == "ok"

        registry.set_enabled(spec.id, True)
        assert registry.get(spec.id).enabled is True  # type: ignore[union-attr]
        assert len(registry.list_all(status=StrategyStatus.VALIDATED, enabled=True)) == 1

    def test_export_import_roundtrip(self) -> None:
        registry = InMemoryStrategyRegistry()
        registry.register(_default_spec())
        registry.set_status("strat-0001", StrategyStatus.INITIAL_BACKTEST)
        payload = registry.export()
        assert len(payload) == 1
        assert payload[0]["spec"]["id"] == "strat-0001"

        other = InMemoryStrategyRegistry()
        assert other.import_(payload) == 1
        restored = other.get("strat-0001")
        assert restored is not None
        assert restored.status is StrategyStatus.INITIAL_BACKTEST
        assert decode_spec(payload[0]["spec"]) == restored.spec

    def test_update_unknown_raises(self) -> None:
        registry = InMemoryStrategyRegistry()
        with pytest.raises(KeyError):
            registry.update(StrategyRecord(spec=_default_spec()))


# --------------------------------------------------------------------------- #
# Robustness
# --------------------------------------------------------------------------- #


def _summary(
    trades: int = 30,
    sharpe: Decimal | None = Decimal("1.2"),
    cagr: Decimal | None = None,
    drawdown: Decimal | None = Decimal("-0.2"),
) -> PerformanceSummary:
    return PerformanceSummary(
        strategy="strat-0001",
        mode=TradingMode.BACKTEST,
        period_start=date(2020, 1, 1),
        period_end=date(2021, 1, 1),
        trades_count=trades,
        sharpe=sharpe,
        cagr=cagr,
        max_drawdown=drawdown,
    )


def _wf_summary(sharpe: str) -> PerformanceSummary:
    return PerformanceSummary(
        strategy="s",
        mode=TradingMode.BACKTEST,
        period_start=date(2020, 1, 1),
        period_end=date(2021, 1, 1),
        trades_count=30,
        sharpe=Decimal(sharpe),
    )


class TestRobustness:
    def test_passes_when_all_ok(self) -> None:
        report = RobustnessChecker().check(_default_spec(), _summary())
        assert report.passed
        assert all(report.checks.values())

    def test_flags_min_trades(self) -> None:
        report = RobustnessChecker().check(_default_spec(), _summary(trades=3))
        assert not report.passed
        assert not report.checks["min_trades"]

    def test_flags_extreme_cagr(self) -> None:
        report = RobustnessChecker().check(_default_spec(), _summary(cagr=Decimal("9.0")))
        assert not report.checks["extreme_performance"]

    def test_flags_narrow_params(self) -> None:
        spec = StrategySpec(
            id="x",
            name="x",
            entry=EntryRule(conditions=(Condition(feature="rsi", op=Operator.GT, value=50.0),)),
            exit=ExitRule(conditions=(Condition(feature="rsi", op=Operator.LT, value=50.5),)),
        )
        report = RobustnessChecker().check(spec, _summary())
        assert not report.checks["narrow_params"]

    def test_flags_instability(self) -> None:
        stable = [_wf_summary("1.2") for _ in range(4)]
        unstable = [_wf_summary(v) for v in ("1.2", "-0.5", "2.0", "-1.0")]
        assert RobustnessChecker().check(_default_spec(), _summary(), stable).passed
        assert not RobustnessChecker().check(_default_spec(), _summary(), unstable).passed


# --------------------------------------------------------------------------- #
# Metric gate
# --------------------------------------------------------------------------- #


class TestMetricGate:
    def test_gate_defaults_reject_underperformer(self) -> None:
        assert not MetricGate().passes(_summary(trades=5, sharpe=None))

    def test_lenient_gate_passes(self) -> None:
        gate = MetricGate(
            min_sharpe=-999.0,
            min_profit_factor=0.0,
            min_win_rate=0.0,
            min_trades=1,
            max_drawdown=-10.0,
        )
        assert gate.passes(_summary())


# --------------------------------------------------------------------------- #
# Walk-forward over a strategy
# --------------------------------------------------------------------------- #


class TestStrategyWalkForward:
    @pytest.mark.asyncio
    async def test_validate_persists_under_strategy_label(self) -> None:
        bars = {
            "AAA": _make_bars("AAA", date(2020, 1, 1), 400, seed=1, drift=0.001),
            "BBB": _make_bars("BBB", date(2020, 1, 1), 400, seed=2, drift=-0.0005),
        }
        prices = _BarsPriceRepository(bars)
        performance = FakePerformanceRepository()
        validator = StrategyWalkForwardValidator(
            prices=prices,
            performance=performance,
            risk_calculator=RiskCalculator(RiskPolicy()),
            strategy=_default_spec(),
        )
        summary = await _run_validator(validator, bars)
        assert summary is not None
        assert summary.strategy == "strat-0001"
        assert any(s.strategy == "strat-0001" for s in performance.summaries)


async def _run_validator(validator, bars_by_symbol) -> PerformanceSummary | None:
    start = min(b.ts.date() for bars in bars_by_symbol.values() for b in bars)
    end = max(b.ts.date() for bars in bars_by_symbol.values() for b in bars)
    return await validator.validate(
        symbols=list(bars_by_symbol),
        start=start,
        end=end,
        initial_capital=Decimal("100000"),
        interval=Interval.D1,
        commission_bps=10.0,
        slippage_bps=50.0,
    )


# --------------------------------------------------------------------------- #
# Research engine
# --------------------------------------------------------------------------- #


def _engine(
    bars_by_symbol, *, plan: ResearchPlan | None = None
) -> tuple[
    StrategyResearchEngine,
    _BarsPriceRepository,
    FakePerformanceRepository,
    InMemoryStrategyRegistry,
]:
    prices = _BarsPriceRepository(bars_by_symbol)
    performance = FakePerformanceRepository()
    registry = InMemoryStrategyRegistry()
    engine = StrategyResearchEngine(
        prices=prices,
        performance=performance,
        risk_calculator=RiskCalculator(RiskPolicy()),
        logs=None,
        plan=plan or ResearchPlan(instability_budget=0),
        registry=registry,
    )
    return engine, prices, performance, registry


def _request(bars_by_symbol) -> ResearchRequest:
    start = min(b.ts.date() for bars in bars_by_symbol.values() for b in bars)
    end = max(b.ts.date() for bars in bars_by_symbol.values() for b in bars)
    return ResearchRequest(
        symbols=tuple(bars_by_symbol),
        start=start,
        end=end,
        interval=Interval.D1,
        dataset_version="test-v1",
    )


class TestResearchEngine:
    def _dataset(self) -> dict[str, list[PriceBar]]:
        return {
            "AAA": _make_bars("AAA", date(2020, 1, 1), 500, seed=1, drift=0.001),
            "BBB": _make_bars("BBB", date(2020, 1, 1), 500, seed=2, drift=-0.0005),
        }

    @pytest.mark.asyncio
    async def test_full_pipeline_reports_counts(self) -> None:
        plan = ResearchPlan(
            limits=SearchLimits(max_strategies=8, computational_budget=8),
            gate=MetricGate(
                min_sharpe=-999.0,
                min_profit_factor=0.0,
                min_win_rate=0.0,
                min_trades=1,
                max_drawdown=-10.0,
            ),
            robustness=RobustnessLimits(
                max_complexity=10,
                min_trades=1,
                max_cagr=999.0,
                max_drawdown=-10.0,
                max_instability=999.0,
            ),
            instability_budget=0,
        )
        engine, _, _, registry = _engine(self._dataset(), plan=plan)
        report = await engine.run(_request(self._dataset()))
        assert isinstance(report, ResearchReport)
        assert report.generated == 8
        assert report.backtests_run <= 8
        assert report.rejected + report.validated == report.generated
        statuses = {r.status for r in registry.list_all()}
        assert StrategyStatus.VALIDATED in statuses or StrategyStatus.REJECTED in statuses

    @pytest.mark.asyncio
    async def test_budget_caps_backtests(self) -> None:
        plan = ResearchPlan(
            limits=SearchLimits(max_strategies=50, computational_budget=3),
            instability_budget=0,
        )
        engine, _, _, _ = _engine(self._dataset(), plan=plan)
        report = await engine.run(_request(self._dataset()))
        assert report.backtests_run <= 3

    @pytest.mark.asyncio
    async def test_strict_gate_rejects_everything(self) -> None:
        plan = ResearchPlan(
            limits=SearchLimits(max_strategies=8, computational_budget=8),
            gate=MetricGate(min_trades=10_000),
            instability_budget=0,
        )
        engine, _, _, registry = _engine(self._dataset(), plan=plan)
        report = await engine.run(_request(self._dataset()))
        assert report.validated == 0
        assert any(v == "failed metric gate" for v in report.rejected_reasons.values())
        assert all(r.status is not StrategyStatus.VALIDATED for r in registry.list_all())

    @pytest.mark.asyncio
    async def test_initial_backtest_is_net_of_costs(self) -> None:
        engine, _, _, _ = _engine(self._dataset(), plan=ResearchPlan(instability_budget=0))
        spec = _default_spec()
        result = await engine._initial_backtest(spec, _request(self._dataset()))  # type: ignore[attr-defined]
        assert result is not None
        assert result.run.commission_bps == Decimal("10")
        assert result.run.slippage_bps == Decimal("50")

    @pytest.mark.asyncio
    async def test_jitter_consumes_instability_budget(self) -> None:
        engine, _, _, registry = _engine(
            self._dataset(),
            plan=ResearchPlan(
                limits=SearchLimits(max_strategies=2, computational_budget=2),
                instability_budget=2,
            ),
        )
        report = await engine.run(_request(self._dataset()))
        assert report.backtests_run >= 2


# --------------------------------------------------------------------------- #
# Settings plan
# --------------------------------------------------------------------------- #


class TestSettingsPlan:
    def test_default_plan(self) -> None:
        from qtrader.config.settings import Settings

        plan = Settings().strategy_research_plan
        assert isinstance(plan, ResearchPlan)
        assert plan.commission_bps == 10.0
        assert plan.slippage_bps == 50.0
        assert plan.limits.max_strategies == 60
        assert plan.initial_capital == Decimal("100000")
