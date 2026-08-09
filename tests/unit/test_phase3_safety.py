"""Phase 3 — safety & risk-gap unit tests.

Validates the failure/risk guarantees measured during final validation and the
fixes that close the Phase 3 gaps:

A) Single-agent failure must never cause unsafe trading: the ensemble HOLDs
   when evidence coverage drops below its floor, and a dead prediction agent
   only ever makes the system LESS aggressive (never more).
B) Risk-manager limits are actually enforceable in the live/paper path: the
   RiskAgent feeds real intraday PnL (so the daily-loss limit fires) and real
   ADV (so the liquidity limit fires); a missing ATR halts instead of sizing
   off a 2% proxy; decision-time prices must be fresh.
C) Stop losses ARE submitted: the paper broker registers a bracket stop for
   every bracketed BUY and simulates the trigger against the last price.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def _decision(decision: Decision = Decision.BUY) -> DecisionMade:
    return DecisionMade(
        decision_uuid="d-1",
        symbol="AAPL",
        decision=decision,
        confidence=0.8,
        rationale="x",
    )


def test_daily_loss_limit_enforced_when_reported() -> None:
    calc = RiskCalculator(RiskPolicy())
    assessment = calc.assess(_inputs(daily_pnl_pct=-0.05))
    assert assessment.approved is False
    assert any("daily loss" in r for r in assessment.rejection_reasons)


def test_adv_limit_skipped_when_none() -> None:
    calc = RiskCalculator(RiskPolicy())
    with_adv = calc.assess(_inputs(adv_daily=Decimal("1000")))
    assert any("ADV" in r for r in with_adv.rejection_reasons)
    # A missing ADV (no volume history) is skipped, not fatal: liquidity
    # cannot be checked without data, but the other limits still apply.
    no_adv = calc.assess(_inputs(adv_daily=None))
    assert no_adv.approved is True


def test_missing_atr_rejected_instead_of_two_percent_proxy() -> None:
    calc = RiskCalculator(RiskPolicy())
    # atr=None used to fall back to 2% of price and trade (risk_calculator
    # pre-fix): now it halts on unreliable indicator data.
    assessment = calc.assess(_inputs(atr=None))
    assert assessment.approved is False
    assert any("no ATR" in r for r in assessment.rejection_reasons)
    # An explicit bracket distance sizes correctly without ATR.
    explicit = calc.assess(_inputs(atr=None, atr_stop_distance=Decimal("3")))
    assert explicit.approved is True
    assert explicit.stop_loss == Decimal("97")


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


def _filled_order(*, side, qty: int, fill: str, created: datetime) -> Order:
    return Order(
        portfolio_id=1,
        stock_id=1,
        side=side,
        order_type=OrderType.MARKET,
        quantity=qty,
        mode=TradingMode.PAPER,
        idempotency_key="k",
        symbol="AAPL",
        status=OrderStatus.FILLED,
        filled_qty=qty,
        avg_fill_price=Money(fill),
        created_at=created,
    )


async def test_agent_enforces_daily_loss_on_todays_realized_loss() -> None:
    # A BUY filled yesterday at $100 and a SELL filled today at $69.90 realize
    # -$3,010 = -3.01% of $100k equity: the live RiskAgent must reject on the
    # daily-loss limit (it used to always feed daily_pnl_pct=0.0 and could
    # never fire).
    now = datetime.now(UTC)
    buy = _filled_order(side=TradeSide.BUY, qty=100, fill="100", created=now - timedelta(days=1))
    sell = _filled_order(
        side=TradeSide.SELL, qty=100, fill="69.9", created=now - timedelta(minutes=6)
    )
    agent = _risk_agent(orders=FakeOrderRepository([buy, sell]))
    assessment = await agent.assess_symbol(_decision())
    assert assessment.approved is False
    assert any("daily loss" in r for r in assessment.rejection_reasons)


class _VolumeHistoryRepository(FakePriceRepository):
    """21 daily bars at $100 with 1,000 shares each -> $100k ADV."""

    async def history(self, symbol, interval, start=None, end=None, limit=500):
        close = Decimal(self._close)
        ts = datetime.now(UTC)
        return [
            PriceBar(
                symbol=symbol,
                interval=interval,
                ts=ts,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("1000"),
            )
            for _ in range(21)
        ]


async def test_agent_enforces_adv_from_volume_history() -> None:
    # ADV = $100k; the sized position (~$33.3k) is 33% of ADV >> 1% limit. The
    # live agent used to pass adv_daily=None so this could never fire.
    agent = _risk_agent(prices=_VolumeHistoryRepository(close="100"))
    assessment = await agent.assess_symbol(_decision())
    assert assessment.approved is False
    assert any("ADV" in r for r in assessment.rejection_reasons)


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


async def test_stale_price_rejected_at_decision_time() -> None:
    # A bar four months old used to be accepted as the entry price: now the
    # decision-time freshness check rejects it.
    agent = _risk_agent(prices=_StalePriceRepository(close="100"))
    assessment = await agent.assess_symbol(_decision())
    assert assessment.approved is False
    assert any("stale price" in r for r in assessment.rejection_reasons)


async def test_fresh_price_still_approved() -> None:
    agent = _risk_agent()
    assessment = await agent.assess_symbol(_decision())
    assert assessment.approved is True


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


async def test_paper_broker_registers_bracket_stop_order() -> None:
    broker = PaperBroker(prices=FakePriceRepository(close="100"))
    broker_order_id = await broker.submit_order(_order_with_brackets())
    # The broker now receives the bracket: a child SELL STOP is registered.
    assert len(broker._orders) == 2
    market = broker._orders[broker_order_id]
    assert market.order_type is OrderType.MARKET
    stop = broker._orders[f"{broker_order_id}-stop"]
    assert stop.order_type is OrderType.STOP
    assert stop.side is TradeSide.SELL
    assert stop.stop_price is not None and stop.stop_price.amount == Decimal("95")
    assert stop.take_profit is not None and stop.take_profit.amount == Decimal("110")
    fill = await broker.get_order_status(broker_order_id)
    assert fill.status is OrderStatus.FILLED


async def test_paper_broker_stop_pending_inside_bracket() -> None:
    broker = PaperBroker(prices=FakePriceRepository(close="100"))
    broker_order_id = await broker.submit_order(_order_with_brackets())
    stop_fill = await broker.get_order_status(f"{broker_order_id}-stop")
    assert stop_fill.status is OrderStatus.PENDING


async def test_paper_broker_stop_fills_through_break() -> None:
    broker = PaperBroker(prices=FakePriceRepository(close="94"))
    broker_order_id = await broker.submit_order(_order_with_brackets())
    stop_fill = await broker.get_order_status(f"{broker_order_id}-stop")
    assert stop_fill.status is OrderStatus.FILLED
    assert stop_fill.avg_fill_price == Decimal("94")


async def test_paper_broker_take_profit_fills_at_target() -> None:
    broker = PaperBroker(prices=FakePriceRepository(close="112"))
    broker_order_id = await broker.submit_order(_order_with_brackets())
    stop_fill = await broker.get_order_status(f"{broker_order_id}-stop")
    assert stop_fill.status is OrderStatus.FILLED
    assert stop_fill.avg_fill_price == Decimal("110")


async def test_execution_submits_market_order_with_bracket_registered() -> None:
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
    market_orders = [o for o in broker._orders.values() if o.order_type is OrderType.MARKET]
    stop_orders = [o for o in broker._orders.values() if o.order_type is OrderType.STOP]
    assert len(market_orders) == 1
    assert len(stop_orders) == 1
    assert stop_orders[0].stop_price is not None and stop_orders[0].stop_price.amount == Decimal(
        "95"
    )
    saved = (await orders.list_by_portfolio(1))[0]
    assert saved.status is OrderStatus.FILLED
