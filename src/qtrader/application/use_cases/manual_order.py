"""ManualOrder — the only write path that bypasses the agent pipeline.

Mirrors the safety of the auto path: the order is risk-checked with the same
:class:`RiskCalculator` and submitted through :class:`ExecutionAgent` (which
enforces the SystemGate + broker), so a manual order can never reach a broker
without passing the same gates as a machine decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from qtrader.application.agents.execution import ExecutionAgent
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.services.risk_calculator import RiskCalculator, RiskInputs
from qtrader.config.settings import Settings
from qtrader.domain.entities import Order, Stock
from qtrader.domain.ports import (
    IndicatorRepository,
    OrderRepository,
    PositionRepository,
    PriceRepository,
    StockRepository,
)
from qtrader.domain.value_objects import (
    Decision,
    Interval,
    Money,
    OrderStatus,
    OrderType,
    TradeSide,
    TradingMode,
)


class OrderRejectedError(Exception):
    """The risk gate refused the manual order."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class NoPriceDataError(Exception):
    """No market data is available for the requested symbol."""


@dataclass(frozen=True, slots=True)
class ManualOrderRequest:
    symbol: str
    side: str
    quantity: int
    order_type: str = "MARKET"
    stop_loss: str | None = None
    take_profit: str | None = None

    @classmethod
    def from_schema(cls, body: Any) -> ManualOrderRequest:
        return cls(
            symbol=body.symbol,
            side=body.side.value if hasattr(body.side, "value") else str(body.side),
            quantity=body.quantity,
            order_type=body.order_type.value
            if hasattr(body.order_type, "value")
            else str(body.order_type),
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
        )


class ManualOrder:
    def __init__(
        self,
        portfolios: PortfolioService,
        stocks: StockRepository,
        prices: PriceRepository,
        indicators: IndicatorRepository,
        positions: PositionRepository,
        orders: OrderRepository,
        risk_calculator: RiskCalculator,
        execution: ExecutionAgent,
        settings: Settings,
    ) -> None:
        self._portfolios = portfolios
        self._stocks = stocks
        self._prices = prices
        self._indicators = indicators
        self._positions = positions
        self._orders = orders
        self._risk = risk_calculator
        self._execution = execution
        self._settings = settings

    async def submit(self, request: ManualOrderRequest) -> Order:
        portfolio = await self._portfolios.default_portfolio()
        portfolio_id = portfolio.portfolio_id
        assert portfolio_id is not None

        symbol = request.symbol.strip().upper()
        stock = await self._stocks.get_by_symbol(symbol)
        if stock is None:
            stock = await self._stocks.upsert(
                Stock(symbol=symbol, exchange="PAPER", name=symbol)
            )
        assert stock.stock_id is not None

        bar = await self._prices.latest(symbol, Interval.D1)
        if bar is None:
            raise NoPriceDataError(
                f"no price data for {symbol} — run the data agent or add {symbol} "
                "to the watchlist first"
            )
        entry = bar.close

        await self._check_risk(
            portfolio_id=portfolio_id,
            symbol=symbol,
            side=request.side,
            quantity=request.quantity,
            entry=entry,
            equity=portfolio.current_cash.amount,
            stop_loss=request.stop_loss,
        )

        mode = self._effective_mode()
        order: Order = await self._orders.create(
            Order(
                portfolio_id=portfolio_id,
                stock_id=stock.stock_id,
                side=TradeSide(request.side),
                order_type=OrderType(request.order_type),
                quantity=request.quantity,
                mode=mode,
                idempotency_key=f"manual:{uuid4()}",
                limit_price=None,
                stop_loss=Money(request.stop_loss) if request.stop_loss else None,
                take_profit=Money(request.take_profit) if request.take_profit else None,
                reason={"manual": True},
                symbol=symbol,
                status=OrderStatus.PENDING,
            )
        )
        await self._execution.execute_order(order)
        return order

    async def _check_risk(
        self,
        *,
        portfolio_id: int,
        symbol: str,
        side: str,
        quantity: int,
        entry: Decimal,
        equity: Decimal,
        stop_loss: str | None,
    ) -> None:
        open_positions = await self._positions.open_positions(portfolio_id)
        exposure = Decimal(0)
        for position in open_positions:
            bar = await self._prices.latest(position.symbol or "", Interval.D1)
            price = bar.close if bar else position.avg_entry_price.amount
            exposure += Decimal(position.quantity) * price
        exposure_pct = float(exposure / equity) if equity else 0.0

        snapshot = await self._indicators.latest(symbol, Interval.D1)
        existing = next(
            (p for p in open_positions if p.symbol == symbol), None
        )
        assessment = self._risk.assess(
            RiskInputs(
                decision=Decision.BUY if side == "BUY" else Decision.SELL,
                symbol=symbol,
                entry_price=entry,
                atr=snapshot.atr if snapshot else None,
                equity=equity,
                current_exposure_pct=exposure_pct,
                open_positions=len(open_positions),
                sector_exposure_pct=0.0,
                adv_daily=None,
                cooldown_remaining_minutes=0.0,
                daily_pnl_pct=0.0,
                trades_today=0,
                position_quantity=Decimal(quantity) if side == "BUY" else (
                    Decimal(existing.quantity) if existing else None
                ),
                position_stop=Decimal(stop_loss) if stop_loss else None,
            )
        )
        if not assessment.approved:
            raise OrderRejectedError(assessment.rejection_reasons)

    def _effective_mode(self) -> TradingMode:
        mode = self._settings.qtrader_mode
        if mode is TradingMode.LIVE and not self._settings.live_enabled:
            raise ValueError("live mode requires ENABLE_LIVE_TRADING=true")
        return mode
