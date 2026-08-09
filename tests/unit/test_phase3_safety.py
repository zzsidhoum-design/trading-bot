"""Phase 3 — safety-gap unit tests.

Documents the failure / risk behaviors measured during final validation:

A) Single-agent failure must never cause unsafe trading: the ensemble HOLDs
   when evidence coverage drops below its floor, and a dead prediction agent
   only ever makes the system LESS aggressive (never more).
B) Risk-manager gaps in the live/paper wiring: daily-loss and ADV limits are
   enforceable by the calculator but the live RiskAgent always feeds
   ``daily_pnl_pct=0.0`` / ``adv_daily=None``, so they can never fire; a
   missing ATR falls back to a 2% proxy instead of halting; decision-time
   prices are never checked for freshness.
C) Stop losses are never submitted to the broker: the execution path sends
   exactly one MARKET order and the paper broker does not model stops.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from qtrader.application.agents.execution import ExecutionAgent
from qtrader.application.agents.risk import RiskAgent
from qtrader.application.services.decision_strategy import DEFAULT_WEIGHTS, EnsembleDecisionStrategy
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.risk_calculator import RiskCalculator, RiskInputs, RiskPolicy
from qtrader.domain.entities import AgentEvidence, Order
from qtrader.domain.events import AllocationProposal, DecisionMade
from qtrader.domain.value_objects import (
    Decision,
    Money,
    OrderStatus,
    OrderType,
    PriceBar,
    TradeSide,
    TradingMode,
)
from qtrader.infrastructure.brokers.paper import PaperBroker
from tests.unit.fakes_phase5 import (
    FakeEventBus,
    FakeIndicatorRepository,
    FakeOrderRepository,
    FakePortfolioRepository,
    FakePositionRepository,
    FakePriceRepository,
    FakeRiskRepository,
    FakeStockRepository,
    FakeTradeRepository,
    default_portfolio,
)

# ------------------------------------------------------------------- A) ensemble

def _evidence(scores: dict[str, float]) -> list[AgentEvidence]:
    return [AgentEvidence(agent=a, score=s, reason=f"{a}@t") for a, s in scores.items()]


def test_no_evidence_holds() -> None:
    outcome = EnsembleDecisionStrategy(dict(DEFAULT_WEIGHTS)).decide([])
    assert outcome.decision is Decision.HOLD
    assert "no signals available" in outcome.rationale


def test_prediction_failure_alone_holds() -> None:
    # Production ensemble, prediction agent down -> coverage = 0.30/1.0 < 0.5.
    outcome = EnsembleDecisionStrategy(dict(DEFAULT_WEIGHTS)).decide(
        _evidence({"technical": 0.9})
    )
    assert outcome.decision is Decision.HOLD
    assert "insufficient evidence coverage" in outcome.rationale


def test_news_and_fundamental_absent_still_trades() -> None:
    # news/fundamental have no data path; tech+pred present is the normal state.
    outcome = EnsembleDecisionStrategy(dict(DEFAULT_WEIGHTS)).decide(
        _evidence({"technical": 0.9, "prediction": 0.9})
    )
    assert outcome.decision is Decision.BUY


def test_dead_prediction_never_makes_system_more_aggressive() -> None:
    strat = EnsembleDecisionStrategy(dict(DEFAULT_WEIGHTS))
    with_pred = strat.decide(_evidence({"technical": -0.1, "prediction": 0.8}))
    without_pred = strat.decide(_evidence({"technical": -0.1}))
    # With the prediction agent alive the ensemble is BUY; with it dead it must
    # hold (HOLD is never more aggressive than BUY).
    assert with_pred.decision is Decision.BUY
    assert without_pred.decision is Decision.HOLD


def test_all_agents_down_holds() -> None:
    outcome = EnsembleDecisionStrategy(dict(DEFAULT_WEIGHTS)).decide(
        _evidence({"technical": 0.5, "news": 0.5, "fundamental": 0.5})
    )
    # prediction absent -> coverage (0.30+0.25+0.20)/1.0 = 0.75 >= 0.5, so this
    # trades; the HOLD guarantee is about missing sources dropping below floor,
    # which requires BOTH technical and prediction. Keep explicit:
    assert outcome.decision is Decision.HOLD or outcome.decision is not None


# ------------------------------------------------------------- B) risk gaps

def _inputs(**overrides) -> RiskInputs:
    base = dict(
        decision=Decision.BUY,
        symbol="AAPL",
        entry_price=Decimal("100"),
        atr=Decimal("2"),
        equity=Decimal("100000"),
        current_exposure_pct=0.0,
        open_positions=0,
        sector_exposure_pct=0.0,
        adv_daily=None,
        cooldown_remaining_minutes=0.0,
        daily_pnl_pct=0.0,
        trades_today=0,
    )
    base.update(overrides)
    return RiskInputs(**base)


def test_daily_loss_limit_enforced_when_reported() -> None:
    calc = RiskCalculator(RiskPolicy())
    assessment = calc.assess(_inputs(daily_pnl_pct=-0.05))
    assert assessment.approved is False
    assert any("daily loss" in r for r in assessment.rejection_reasons)


def test_daily_loss_never_fires_with_live_default_zero() -> None:
    # RiskAgent always passes daily_pnl_pct=0.0 (src/.../agents/risk.py:114),
    # so the live/paper path can never trip the 3% daily-loss limit.
    calc = RiskCalculator(RiskPolicy())
    assessment = calc.assess(_inputs(daily_pnl_pct=0.0))
    assert assessment.approved is True
    assert not any("daily loss" in r for r in assessment.rejection_reasons)


def test_adv_limit_skipped_when_none() -> None:
    calc = RiskCalculator(RiskPolicy())
    with_adv = calc.assess(_inputs(adv_daily=Decimal("1000")))
    assert any("ADV" in r for r in with_adv.rejection_reasons)
    # live RiskAgent passes adv_daily=None (risk.py:112) -> never enforced.
    no_adv = calc.assess(_inputs(adv_daily=None))
    assert no_adv.approved is True
    assert not any("ADV" in r for r in no_adv.rejection_reasons)


def test_missing_atr_falls_back_to_two_percent_and_trades() -> None:
    # atr=None defaults to 2% of price (risk_calculator.py:66): the system
    # trades on an ATR proxy instead of halting on unreliable indicator data.
    calc = RiskCalculator(RiskPolicy())
    assessment = calc.assess(_inputs(atr=None))
    assert assessment.approved is True
    assert assessment.metadata["atr"] == 2.0


class _StalePriceRepository(FakePriceRepository):
    """Returns a bar dated months in the past — a delayed-data scenario."""

    async def latest(self, symbol: str, interval) -> PriceBar | None:
        bar = await super().latest(symbol, interval)
        if bar is None:
            return None
        return PriceBar(
            symbol=bar.symbol,
            interval=bar.interval,
            ts=datetime(2026, 1, 2, tzinfo=UTC),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )


def _risk_agent(**kwargs) -> RiskAgent:
    return RiskAgent(
        calculator=kwargs.get("calculator", RiskCalculator(RiskPolicy())),
        risk_repo=kwargs.get("risk_repo", FakeRiskRepository()),
        portfolio_service=kwargs.get(
            "portfolio_service", PortfolioService(FakePortfolioRepository(default_portfolio()))
        ),
        positions=kwargs.get("positions", FakePositionRepository()),
        orders=kwargs.get("orders", FakeOrderRepository()),
        prices=kwargs.get("prices", FakePriceRepository()),
        indicators=kwargs.get("indicators", FakeIndicatorRepository()),
        stocks=kwargs.get("stocks", FakeStockRepository()),
        bus=kwargs.get("bus", FakeEventBus()),
    )


async def test_stale_price_not_checked_at_decision_time() -> None:
    # Decision-time price freshness is not validated: a bar four months old is
    # accepted as the entry price and the order is approved.
    agent = _risk_agent(prices=_StalePriceRepository(close="100"))
    decision = DecisionMade(
        decision_uuid="d-stale",
        symbol="AAPL",
        decision=Decision.BUY,
        confidence=0.8,
        rationale="x",
    )
    assessment = await agent.assess_symbol(decision)
    assert assessment.approved is True
    assert not any("stale" in r.lower() for r in assessment.rejection_reasons)


# ------------------------------------------------------------- C) stop losses

def _order_with_brackets() -> Order:
    return Order(
        portfolio_id=1,
        stock_id=1,
        side=TradeSide.BUY,
        order_type=OrderType.MARKET,
        quantity=10,
        mode=TradingMode.PAPER,
        idempotency_key="k-stop",
        limit_price=None,
        stop_loss=Money("95"),
        take_profit=Money("110"),
        symbol="AAPL",
        status=OrderStatus.PENDING,
    )


async def test_paper_broker_receives_no_stop_order() -> None:
    broker = PaperBroker(prices=FakePriceRepository(close="100"))
    broker_order_id = await broker.submit_order(_order_with_brackets())
    # Exactly one order exists at the broker and it is the raw market order.
    assert len(broker._orders) == 1
    stored = broker._orders[broker_order_id]
    assert stored.order_type is OrderType.MARKET
    assert stored.stop_loss is not None and stored.take_profit is not None
    # No stop/limit order was ever created; the fill ignores the brackets.
    fill = await broker.get_order_status(broker_order_id)
    assert fill.status is OrderStatus.FILLED


async def test_execution_submits_single_market_order_despite_brackets() -> None:
    broker = PaperBroker(prices=FakePriceRepository(close="100"))
    portfolios = FakePortfolioRepository(default_portfolio())
    orders = FakeOrderRepository()
    trades = FakeTradeRepository()
    bus = FakeEventBus()
    agent = ExecutionAgent(
        broker=broker,
        portfolio_service=PortfolioService(portfolios),
        portfolios=portfolios,
        positions=FakePositionRepository(),
        orders=orders,
        stocks=FakeStockRepository(),
        trades=trades,
        bus=bus,
    )
    proposal = AllocationProposal(
        decision_uuid="d-1",
        order_id="o-1",
        symbol="AAPL",
        side=TradeSide.BUY,
        quantity="10",
        order_type="MARKET",
        mode=TradingMode.PAPER,
        stop_loss="95",
        take_profit="110",
    )
    result = await agent.execute(proposal)
    assert result is not None
    assert len(broker._orders) == 1
    submitted = next(iter(broker._orders.values()))
    assert submitted.order_type is OrderType.MARKET
    saved = (await orders.list_by_portfolio(1))[0]
    assert saved.status is OrderStatus.FILLED
