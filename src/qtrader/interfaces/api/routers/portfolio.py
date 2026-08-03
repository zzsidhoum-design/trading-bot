"""Portfolio & orders router — read side plus gated manual orders (Phase 7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from qtrader.application.services.dashboard_service import DashboardService
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.use_cases.manual_order import ManualOrder, ManualOrderRequest
from qtrader.domain.entities import Order, OrderStatus
from qtrader.domain.exceptions import NotFoundError
from qtrader.domain.ports import OrderRepository, PortfolioRepository
from qtrader.domain.value_objects import TradingMode
from qtrader.interfaces.api.dependencies import (
    get_dashboard_service,
    get_manual_order,
    get_order_repository,
    get_portfolio_repository,
    get_portfolio_service,
    require_api_key,
)
from qtrader.interfaces.api.schemas import (
    OrderCreate,
    OrderOut,
    PerformanceSummaryOut,
    PortfolioSummary,
)

router = APIRouter(prefix="/api/v1/portfolio", tags=["portfolio"])


def _order_out(order: Order) -> OrderOut:
    return OrderOut(
        order_id=order.order_id,
        idempotency_key=order.idempotency_key,
        symbol=order.symbol or "",
        side=order.side.value,
        order_type=order.order_type.value,
        quantity=str(order.quantity),
        mode=order.mode.value,
        status=order.status.value,
        stop_loss=str(order.stop_loss.amount) if order.stop_loss is not None else None,
        take_profit=str(order.take_profit.amount) if order.take_profit is not None else None,
        filled_qty=order.filled_qty,
        avg_fill_price=str(order.avg_fill_price.amount)
        if order.avg_fill_price is not None
        else None,
        created_at=order.created_at,
        decision_ref=order.decision_ref,
    )


@router.get(
    "",
    response_model=PortfolioSummary,
    dependencies=[Depends(require_api_key)],
)
async def portfolio_summary(
    portfolios: PortfolioService = Depends(get_portfolio_service),
    portfolio_repo: PortfolioRepository = Depends(get_portfolio_repository),
) -> PortfolioSummary:
    portfolio = await portfolios.default_portfolio()
    if portfolio is None:
        raise NotFoundError("portfolio not found")
    return PortfolioSummary(
        name=portfolio.name,
        currency=portfolio.currency,
        mode=portfolio.mode,
        status=portfolio.status,
        initial_capital=str(portfolio.initial_capital.amount),
        current_cash=str(portfolio.current_cash.amount),
    )


@router.get(
    "/orders",
    response_model=list[OrderOut],
    dependencies=[Depends(require_api_key)],
)
async def list_orders(
    status: OrderStatus | None = None,
    limit: int = 100,
    portfolios: PortfolioService = Depends(get_portfolio_service),
    order_repo: OrderRepository = Depends(get_order_repository),
) -> list[OrderOut]:
    default = await portfolios.default_portfolio()
    orders = await order_repo.list_by_portfolio(
        default.portfolio_id or 1, status, min(limit, 500)
    )
    return [_order_out(o) for o in orders]


@router.post(
    "/orders",
    response_model=OrderOut,
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def submit_order(
    body: OrderCreate,
    manual_order: ManualOrder = Depends(get_manual_order),
) -> OrderOut:
    order = await manual_order.submit(ManualOrderRequest.from_schema(body))
    return _order_out(order)


@router.get(
    "/performance",
    response_model=list[PerformanceSummaryOut],
    dependencies=[Depends(require_api_key)],
)
async def portfolio_performance(
    strategy: str | None = None,
    mode: TradingMode | None = None,
    limit: int = 50,
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[PerformanceSummaryOut]:
    results = await dashboard.performance(
        strategy=strategy,
        mode=mode,
        limit=limit,
    )
    return [
        PerformanceSummaryOut(
            strategy=p.strategy,
            mode=p.mode.value,
            period_start=p.period_start,
            period_end=p.period_end,
            total_return=str(p.total_return) if p.total_return is not None else None,
            sharpe=str(p.sharpe) if p.sharpe is not None else None,
            sortino=str(p.sortino) if p.sortino is not None else None,
            max_drawdown=str(p.max_drawdown) if p.max_drawdown is not None else None,
            win_rate=str(p.win_rate) if p.win_rate is not None else None,
            profit_factor=str(p.profit_factor) if p.profit_factor is not None else None,
            trades_count=p.trades_count,
            final_equity=str(p.final_equity) if p.final_equity is not None else None,
        )
        for p in results
    ]
