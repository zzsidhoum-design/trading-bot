"""Phase 1 research-infrastructure tests: indicator layer + integration adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from qtrader.application.research import (
    BacktestAdapter,
    IndicatorAdapter,
    MarketDataAdapter,
    PortfolioAdapter,
    PredictionAdapter,
    StrategyAdapter,
)
from qtrader.application.services.backtest import BacktestParams
from qtrader.application.services.indicators import (
    ADX,
    ATR,
    MACD,
    RSI,
    VWAP,
    BollingerBands,
    ExponentialMovingAverage,
    IndicatorEngine,
    SimpleMovingAverage,
    Stochastic,
    frame_from_bars,
    indicator_factory,
    indicator_names,
)
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.prediction_model import HeuristicModel, LogisticModel
from qtrader.application.services.strategies.base import Strategy, StrategyInputs
from qtrader.domain.entities import Portfolio, Position, RegisteredModel
from qtrader.domain.value_objects import (
    Interval,
    Money,
    PositionStatus,
    PriceBar,
    TradingMode,
)
from tests.unit.fakes_phase7 import (
    FakeBacktestRunner,
    FakeModelRepository,
    FakePortfolioRepository,
    FakePositionRepository,
    FakePriceRepository,
)

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _series_bars(closes: list[float]) -> list[PriceBar]:
    bars: list[PriceBar] = []
    for i, close in enumerate(closes):
        bars.append(
            PriceBar(
                symbol="TEST",
                interval=Interval.M5,
                ts=BASE + timedelta(minutes=5 * i),
                open=Decimal(str(close)),
                high=Decimal(str(close + 2)),
                low=Decimal(str(close - 2)),
                close=Decimal(str(close)),
                volume=Decimal("1000"),
            )
        )
    return bars


CLOSES = [10.0, 11.0, 12.0, 13.0, 14.0]


# --------------------------------------------------------------------------- #
# Indicator factory / registry
# --------------------------------------------------------------------------- #


def test_indicator_factory_known_names() -> None:
    assert isinstance(indicator_factory("sma", period=5), SimpleMovingAverage)
    assert isinstance(indicator_factory("ema", period=5), ExponentialMovingAverage)
    assert isinstance(indicator_factory("rsi", period=14), RSI)
    assert isinstance(indicator_factory("macd"), MACD)
    assert isinstance(indicator_factory("atr", period=14), ATR)
    assert isinstance(indicator_factory("adx", period=14), ADX)
    assert isinstance(indicator_factory("bollinger"), BollingerBands)
    assert isinstance(indicator_factory("vwap"), VWAP)
    assert isinstance(indicator_factory("stochastic"), Stochastic)


def test_indicator_factory_is_case_insensitive() -> None:
    assert isinstance(indicator_factory("SMA", period=5), SimpleMovingAverage)


def test_indicator_factory_passes_params() -> None:
    ema = indicator_factory("ema", period=10)
    assert isinstance(ema, ExponentialMovingAverage)
    assert ema.name == "ema_10"
    assert ema.period == 10
    bb = indicator_factory("bollinger", period=30, std_dev=2.5)
    assert isinstance(bb, BollingerBands)
    assert bb.period == 30
    assert bb.std_dev == 2.5
    stoch = indicator_factory("stochastic", k_period=5, d_period=2)
    assert isinstance(stoch, Stochastic)
    assert stoch.k_period == 5
    assert stoch.d_period == 2


def test_indicator_factory_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown indicator"):
        indicator_factory("sar")


def test_indicator_names_covers_required_set() -> None:
    names = indicator_names()
    for required in ("sma", "ema", "rsi", "macd", "atr", "adx", "bollinger", "vwap", "stochastic"):
        assert required in names


# --------------------------------------------------------------------------- #
# Indicator correctness (hand-computed anchors on a known series)
# --------------------------------------------------------------------------- #


def test_sma_known_values() -> None:
    df = frame_from_bars(_series_bars(CLOSES))
    out = indicator_factory("sma", period=3).compute(df)["sma_3"]
    assert out.iloc[2] == pytest.approx(11.0)
    assert out.iloc[3] == pytest.approx(12.0)
    assert out.iloc[4] == pytest.approx(13.0)
    assert out.iloc[:2].isna().all()


def test_ema_known_values() -> None:
    df = frame_from_bars(_series_bars(CLOSES))
    out = indicator_factory("ema", period=3).compute(df)["ema_3"]
    assert out.iloc[0] == pytest.approx(10.0)
    assert out.iloc[1] == pytest.approx(10.5)
    assert out.iloc[2] == pytest.approx(11.25)
    assert out.iloc[3] == pytest.approx(12.125)
    assert out.iloc[4] == pytest.approx(13.0625)


def test_vwap_known_values() -> None:
    df = frame_from_bars(_series_bars(CLOSES))
    out = indicator_factory("vwap").compute(df)["vwap"]
    assert out.iloc[2] == pytest.approx(11.0)
    assert out.iloc[4] == pytest.approx(12.0)


def test_bollinger_known_values() -> None:
    df = frame_from_bars(_series_bars(CLOSES))
    out = indicator_factory("bollinger", period=3, std_dev=2.0).compute(df)
    assert out["boll_middle"].iloc[4] == pytest.approx(13.0)
    assert out["boll_upper"].iloc[4] == pytest.approx(15.0)
    assert out["boll_lower"].iloc[4] == pytest.approx(11.0)


def test_stochastic_anchor() -> None:
    df = frame_from_bars(_series_bars(CLOSES))
    out = indicator_factory("stochastic", k_period=3, d_period=3).compute(df)
    assert out["stoch_k"].iloc[2] == pytest.approx(100 * 4 / 6, abs=0.01)
    assert out["stoch_d"].iloc[4] == pytest.approx(100 * 4 / 6, abs=0.01)


def test_rsi_rising_series_approaches_100() -> None:
    bars = _series_bars([10.0 + i for i in range(260)])
    out = indicator_factory("rsi", period=14).compute(frame_from_bars(bars))["rsi"]
    valid = out.dropna()
    assert ((valid >= 0) & (valid <= 100)).all()
    assert valid.iloc[-1] == pytest.approx(100.0, abs=1e-6)


def test_rsi_falling_series_approaches_0() -> None:
    bars = _series_bars([520.0 - i for i in range(260)])
    out = indicator_factory("rsi", period=14).compute(frame_from_bars(bars))["rsi"]
    assert out.dropna().iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_macd_matches_ema_difference() -> None:
    df = frame_from_bars(_series_bars(CLOSES))
    out = indicator_factory("macd", fast=2, slow=4).compute(df)
    assert out["macd"].iloc[4] == pytest.approx(0.8118, abs=1e-3)
    assert (out["macd_hist"] == out["macd"] - out["macd_signal"]).all()


def test_atr_known_values() -> None:
    df = frame_from_bars(_series_bars(CLOSES))
    out = indicator_factory("atr", period=3).compute(df)["atr"]
    assert out.iloc[2] == pytest.approx(4.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_adx_bounds_on_trending_series() -> None:
    bars = _series_bars([10.0 + i for i in range(260)])
    out = indicator_factory("adx", period=14).compute(frame_from_bars(bars))["adx"]
    valid = out.dropna()
    assert ((valid >= 0) & (valid <= 100)).all()
    assert valid.iloc[-1] > 90.0


# --------------------------------------------------------------------------- #
# Integration adapters
# --------------------------------------------------------------------------- #


async def test_market_data_adapter_delegates() -> None:
    bar = _series_bars(CLOSES)[-1]
    adapter = MarketDataAdapter(prices=FakePriceRepository(latest_bar=bar))
    assert await adapter.latest("TEST", Interval.M5) is bar
    assert await adapter.history("TEST", Interval.M5) == [bar]


async def test_indicator_adapter_matches_engine() -> None:
    bars = _series_bars([10.0 + i for i in range(120)])
    engine = IndicatorEngine()
    adapter = IndicatorAdapter(engine=engine)
    snap = adapter.compute(bars, "TEST", Interval.M5)
    assert snap.rsi == engine.compute(bars, "TEST", Interval.M5).rsi
    assert len(adapter.series(bars, "TEST", Interval.M5)) == len(bars)


async def test_backtest_adapter_delegates() -> None:
    fake = FakeBacktestRunner()
    adapter = BacktestAdapter(runner=fake)  # type: ignore[arg-type]
    result = await adapter.run(
        name="phase1",
        symbols=["TEST"],
        start=datetime(2026, 1, 1, tzinfo=UTC).date(),
        end=datetime(2026, 2, 1, tzinfo=UTC).date(),
        initial_capital=Decimal("100000"),
        params=BacktestParams(),
    )
    assert result.run.name == "phase1"


def _portfolio() -> Portfolio:
    return Portfolio(
        name="default",
        current_cash=Money("50000"),
        mode=TradingMode.BACKTEST,
        portfolio_id=1,
    )


async def test_portfolio_adapter_default_portfolio_and_cash() -> None:
    adapter = PortfolioAdapter(
        service=PortfolioService(FakePortfolioRepository(portfolio=_portfolio())),
        positions_repo=FakePositionRepository(),
        portfolios_repo=FakePortfolioRepository(portfolio=_portfolio()),
    )
    portfolio = await adapter.default_portfolio()
    assert portfolio.portfolio_id == 1
    assert (await adapter.cash(1)).amount == Decimal("50000")


async def test_portfolio_adapter_positions() -> None:
    position = Position(
        portfolio_id=1,
        stock_id=1,
        quantity=10,
        avg_entry_price=Money("50"),
        status=PositionStatus.OPEN,
    )
    adapter = PortfolioAdapter(
        service=PortfolioService(FakePortfolioRepository(portfolio=_portfolio())),
        positions_repo=FakePositionRepository(positions=[position]),
        portfolios_repo=FakePortfolioRepository(portfolio=_portfolio()),
    )
    assert await adapter.positions(1) == [position]


async def test_portfolio_adapter_cash_raises_when_missing() -> None:
    adapter = PortfolioAdapter(
        service=PortfolioService(FakePortfolioRepository()),
        positions_repo=FakePositionRepository(),
        portfolios_repo=FakePortfolioRepository(),
    )
    with pytest.raises(ValueError, match="no portfolio"):
        await adapter.cash(1)


def test_prediction_adapter_predicts() -> None:
    adapter = PredictionAdapter(model=HeuristicModel())
    out = adapter.predict({"ret_5": 0.05, "rsi": 55.0})
    assert 0.0 <= out.prob_up <= 1.0


async def test_prediction_adapter_from_registry_heuristic_fallback() -> None:
    adapter = await PredictionAdapter.from_registry(
        FakeModelRepository(), "prediction"
    )
    assert isinstance(adapter.model, HeuristicModel)
    assert adapter.predict({"ret_5": 0.0}).prob_up == pytest.approx(0.5, abs=0.01)


async def test_prediction_adapter_from_registry_logistic() -> None:
    repo = FakeModelRepository(
        models=[
            RegisteredModel(
                name="prediction",
                version=1,
                is_active=True,
                hyperparams={
                    "feature_names": ["ret_5"],
                    "coef": [1.0],
                    "intercept": 0.0,
                    "mean": [0.0],
                    "std": [1.0],
                },
            )
        ]
    )
    adapter = await PredictionAdapter.from_registry(repo, "prediction")
    assert isinstance(adapter.model, LogisticModel)
    assert adapter.predict({"ret_5": 0.0}).prob_up == pytest.approx(0.5, abs=1e-3)


class _FakeStrategy(Strategy):
    name = "fake"
    kind = "research"

    def probs_for_symbol(
        self, inputs: StrategyInputs, symbol: str
    ) -> dict[datetime, float]:
        return {}


def test_strategy_adapter_delegates() -> None:
    strategy = _FakeStrategy()
    adapter = StrategyAdapter(strategy=strategy)
    assert adapter.name == "fake"
    inputs = StrategyInputs(bars_by_symbol={}, series_by_symbol={}, oos={})
    assert adapter.generate_probs(inputs) == {}
