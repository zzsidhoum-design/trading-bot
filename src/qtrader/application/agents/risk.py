"""Risk Agent — the capital-safety gate for every Chief decision (docs/02-agents.md §7).

Consumes ``DecisionMade``, gathers live context (portfolio equity, open
positions, sector concentration, price, ATR, cooldown, daily trade count) and
runs the pure :class:`RiskCalculator`. The outcome is persisted to
``risk_history`` and either a ``RiskApproved(order_plan)`` or a
``RiskRejected(reasons)`` event is published on the bus.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

from qtrader.application.agents.base import AgentBase, AgentContext
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.risk_calculator import RiskCalculator, RiskInputs
from qtrader.domain.entities import Position, RiskAssessment
from qtrader.domain.events import DecisionMade, DomainEvent, RiskApproved, RiskRejected
from qtrader.domain.ports import (
    EventBus,
    IndicatorRepository,
    OrderRepository,
    PositionRepository,
    PriceRepository,
    RiskRepository,
    StockRepository,
)
from qtrader.domain.value_objects import (
    Interval,
    OrderPlan,
    OrderStatus,
    OrderType,
    Percentage,
    PriceBar,
    TradeSide,
)


def _age_description(ts: datetime) -> str:
    seconds = int((datetime.now(UTC) - ts).total_seconds())
    if seconds >= 24 * 3600:
        return f"{seconds // (24 * 3600)}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{max(seconds, 0)}s"


class RiskAgent(AgentBase):
    name: ClassVar[str] = "risk"
    consumes: ClassVar[tuple[type[DomainEvent], ...]] = (DecisionMade,)
    produces: ClassVar[tuple[type[DomainEvent], ...]] = (RiskApproved, RiskRejected)

    # Max acceptable age of the decision-time price bar, per interval. D1 bars
    # are allowed to be a few days old (weekends/holidays); anything older is
    # treated as a data outage and the order is rejected rather than priced
    # off a stale print.
    _MAX_BAR_AGE_SECONDS: ClassVar[dict[Interval, int]] = {
        Interval.M1: 300,
        Interval.M5: 900,
        Interval.M15: 1800,
        Interval.H1: 3600,
        Interval.D1: 5 * 24 * 3600,
    }

    def __init__(
        self,
        calculator: RiskCalculator,
        risk_repo: RiskRepository,
        portfolio_service: PortfolioService,
        positions: PositionRepository,
        orders: OrderRepository,
        prices: PriceRepository,
        indicators: IndicatorRepository,
        stocks: StockRepository,
        bus: EventBus,
    ) -> None:
        self._calculator = calculator
        self._risk_repo = risk_repo
        self._portfolios = portfolio_service
        self._positions = positions
        self._orders = orders
        self._prices = prices
        self._indicators = indicators
        self._stocks = stocks
        self._bus = bus

    async def assess_symbol(self, decision: DecisionMade) -> RiskAssessment:
        portfolio = await self._portfolios.default_portfolio()
        portfolio_id = portfolio.portfolio_id
        assert portfolio_id is not None

        open_positions = await self._positions.open_positions(portfolio_id)
        entry_bar = await self._current_bar(decision.symbol)
        atr = await self._current_atr(decision.symbol)

        equity, exposure_pct = self._equity_and_exposure(
            portfolio.current_cash.amount, open_positions
        )
        sector_pct = await self._sector_exposure_pct(open_positions, equity)
        cooldown_remaining, trades_today = await self._activity(portfolio_id, decision.symbol)
        position_qty, position_stop = self._existing_position(open_positions, decision.symbol)

        if entry_bar is None:
            assessment = RiskAssessment(
                decision_uuid=decision.decision_uuid,
                symbol=decision.symbol,
                approved=False,
                rejection_reasons=["no price data for symbol"],
                portfolio_id=portfolio_id,
                metadata={"entry_price": None},
            )
            await self._risk_repo.record(assessment)
            await self._bus.publish(
                RiskRejected(
                    decision_uuid=decision.decision_uuid,
                    symbol=decision.symbol,
                    reasons=assessment.rejection_reasons,
                )
            )
            return assessment

        entry_price = entry_bar.close
        if self._is_stale(entry_bar):
            assessment = RiskAssessment(
                decision_uuid=decision.decision_uuid,
                symbol=decision.symbol,
                approved=False,
                rejection_reasons=[
                    f"stale price data (last {entry_bar.interval.value} bar "
                    f"{entry_bar.ts.isoformat()} is {_age_description(entry_bar.ts)} old)"
                ],
                portfolio_id=portfolio_id,
                metadata={
                    "entry_price": float(entry_price),
                    "last_bar_ts": entry_bar.ts.isoformat(),
                },
            )
            await self._risk_repo.record(assessment)
            await self._bus.publish(
                RiskRejected(
                    decision_uuid=decision.decision_uuid,
                    symbol=decision.symbol,
                    reasons=assessment.rejection_reasons,
                )
            )
            return assessment

        inputs = RiskInputs(
            decision=decision.decision,
            symbol=decision.symbol,
            entry_price=entry_price,
            atr=atr,
            equity=equity,
            current_exposure_pct=exposure_pct,
            open_positions=len(open_positions),
            sector_exposure_pct=sector_pct,
            adv_daily=await self._adv_daily(decision.symbol),
            cooldown_remaining_minutes=cooldown_remaining,
            daily_pnl_pct=await self._daily_pnl_pct(portfolio_id, open_positions, equity),
            trades_today=trades_today,
            position_quantity=position_qty,
            position_stop=position_stop,
        )
        assessment = replace(
            self._calculator.assess(inputs),
            decision_uuid=decision.decision_uuid,
            portfolio_id=portfolio_id,
        )
        await self._risk_repo.record(assessment)
        await self._emit(decision, assessment, entry_price)
        return assessment

    async def _emit(
        self, decision: DecisionMade, assessment: RiskAssessment, entry_price: Decimal
    ) -> None:
        if not assessment.approved:
            await self._bus.publish(
                RiskRejected(
                    decision_uuid=decision.decision_uuid,
                    symbol=decision.symbol,
                    reasons=assessment.rejection_reasons,
                )
            )
            return

        plan = OrderPlan(
            symbol=assessment.symbol,
            side=TradeSide.BUY if decision.decision.value == "BUY" else TradeSide.SELL,
            quantity=(
                assessment.position_size
                if assessment.position_size is not None
                else Decimal(0)
            ),
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_loss=(
                assessment.stop_loss if assessment.stop_loss is not None else entry_price
            ),
            take_profit=(
                assessment.take_profit if assessment.take_profit is not None else entry_price
            ),
            risk_per_trade=Percentage(assessment.risk_per_trade_pct or Decimal(0)),
            estimated_exposure=Percentage(assessment.exposure_pct or Decimal(0)),
            entry_price=entry_price,
        )
        self._logger.info(
            "risk.approved",
            symbol=assessment.symbol,
            qty=str(plan.quantity),
            stop=str(plan.stop_loss),
            tp=str(plan.take_profit),
        )
        await self._bus.publish(RiskApproved(decision_uuid=decision.decision_uuid, plan=plan))

    async def _current_bar(self, symbol: str) -> PriceBar | None:
        bar = await self._prices.latest(symbol, Interval.D1)
        if bar is None:
            bar = await self._prices.latest(symbol, Interval.M5)
        return bar

    def _is_stale(self, bar: PriceBar) -> bool:
        max_age = self._MAX_BAR_AGE_SECONDS.get(bar.interval, 3600)
        return (datetime.now(UTC) - bar.ts).total_seconds() > max_age

    async def _adv_daily(self, symbol: str) -> Decimal | None:
        bars = await self._prices.history(symbol, Interval.D1, limit=21)
        if not bars:
            return None
        dollar_volume = sum(bar.close * bar.volume for bar in bars)
        return dollar_volume / Decimal(len(bars))

    async def _daily_pnl_pct(
        self, portfolio_id: int, open_positions: list[Position], equity: Decimal
    ) -> float:
        today = datetime.now(UTC).date()
        realized = Decimal(0)
        buy_fills: dict[str, list[tuple[datetime, Decimal]]] = {}
        for order in sorted(
            await self._orders.list_by_portfolio(portfolio_id, limit=500),
            key=lambda o: o.created_at,
        ):
            if order.status is not OrderStatus.FILLED or order.avg_fill_price is None:
                continue
            created = order.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if order.side is TradeSide.BUY:
                buy_fills.setdefault(order.symbol or "", []).append(
                    (created, order.avg_fill_price.amount)
                )
            elif created.date() == today:
                entries = buy_fills.get(order.symbol or "")
                if entries:
                    _, entry = entries.pop(0)
                    realized += (
                        order.avg_fill_price.amount - entry
                    ) * Decimal(order.filled_qty)

        mtm = Decimal(0)
        for pos in open_positions:
            bars = await self._prices.history(pos.symbol or "", Interval.D1, limit=2)
            if len(bars) >= 2:
                mtm += Decimal(pos.quantity) * (bars[-1].close - bars[-2].close)
            elif len(bars) == 1:
                mtm += Decimal(pos.quantity) * (bars[-1].close - pos.avg_entry_price.amount)

        total = realized + mtm
        return float(total / equity) if equity else 0.0

    async def _current_atr(self, symbol: str) -> Decimal | None:
        snapshot = await self._indicators.latest(symbol, Interval.D1)
        if snapshot is None:
            snapshot = await self._indicators.latest(symbol, Interval.M5)
        return snapshot.atr if snapshot is not None else None

    def _equity_and_exposure(
        self, cash: Decimal, positions: list[Position]
    ) -> tuple[Decimal, float]:
        equity = cash
        for pos in positions:
            equity += Decimal(pos.quantity) * pos.avg_entry_price.amount
        if equity <= 0:
            return equity, 1.0
        notional = sum(
            Decimal(abs(pos.quantity)) * pos.avg_entry_price.amount for pos in positions
        )
        return equity, float(notional / equity)

    async def _sector_exposure_pct(
        self, positions: list[Position], equity: Decimal
    ) -> float:
        if equity <= 0:
            return 1.0
        sectors: dict[str, Decimal] = {}
        for pos in positions:
            sector = await self._sector_for(pos.symbol)
            notional = Decimal(abs(pos.quantity)) * pos.avg_entry_price.amount
            sectors[sector] = sectors.get(sector, Decimal(0)) + notional
        if not sectors:
            return 0.0
        return float(max(sectors.values()) / equity)

    async def _sector_for(self, symbol: str | None) -> str:
        if symbol is None:
            return "unknown"
        stock = await self._stocks.get_by_symbol(symbol)
        return stock.sector if stock is not None and stock.sector else "unknown"

    async def _activity(self, portfolio_id: int, symbol: str) -> tuple[float, int]:
        orders = await self._orders.list_by_portfolio(portfolio_id, limit=200)
        now = datetime.now(UTC)
        cooldown = 0.0
        trades_today = 0
        for order in orders:
            created = order.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if (
                created.date() == now.date()
                and order.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
            ):
                trades_today += 1
            if order.symbol == symbol and order.status in (
                OrderStatus.FILLED,
                OrderStatus.SUBMITTED,
            ):
                elapsed = (now - created).total_seconds() / 60.0
                remaining = self._calculator.policy.min_cooldown_minutes - elapsed
                if remaining > cooldown:
                    cooldown = max(remaining, 0.0)
        return cooldown, trades_today

    def _existing_position(
        self, positions: list[Position], symbol: str
    ) -> tuple[Decimal | None, Decimal | None]:
        for pos in positions:
            if pos.symbol == symbol and pos.quantity > 0:
                return (
                    Decimal(pos.quantity),
                    pos.stop_loss.amount if pos.stop_loss is not None else None,
                )
        return None, None

    async def on_event(self, event: DomainEvent) -> None:
        if isinstance(event, DecisionMade):
            try:
                await self.assess_symbol(event)
            except Exception:
                self._logger.exception("risk.assess_failed", symbol=event.symbol)

    async def run(self, ctx: AgentContext) -> None:
        self._logger.warning("risk.run_standalone", detail="Risk agent is event-driven only")
