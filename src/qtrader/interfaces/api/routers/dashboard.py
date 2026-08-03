"""Dashboard read router (Phase 7) — read-only aggregation for the UI."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from qtrader.application.services.dashboard_service import DashboardService
from qtrader.domain.exceptions import NotFoundError
from qtrader.domain.ports import RiskRepository
from qtrader.interfaces.api.dependencies import (
    get_dashboard_service,
    get_risk_repository,
    require_api_key,
)
from qtrader.interfaces.api.schemas import (
    AgentMetricOut,
    AllocationOut,
    DashboardSummary,
    EquityPoint,
    LogOut,
    PerformanceSummaryOut,
    PositionOut,
    RiskAssessmentOut,
    TopStockOut,
    TradeOut,
)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get(
    "/summary",
    response_model=DashboardSummary,
    dependencies=[Depends(require_api_key)],
)
async def summary(
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> DashboardSummary:
    result = await dashboard.summary()
    if result is None:
        raise NotFoundError("portfolio not found")
    return DashboardSummary(
        cash=str(result.cash),
        equity=str(result.equity),
        open_positions=result.open_positions,
        unrealized_pnl=str(result.unrealized_pnl),
        exposure_pct=result.exposure_pct,
        total_trades=result.total_trades,
    )


@router.get(
    "/equity",
    response_model=list[EquityPoint],
    dependencies=[Depends(require_api_key)],
)
async def equity(
    limit: int = 200,
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[EquityPoint]:
    points = await dashboard.equity_curve(limit=limit)
    return [EquityPoint(ts=p.ts, equity=str(p.equity)) for p in points]


@router.get(
    "/positions",
    response_model=list[PositionOut],
    dependencies=[Depends(require_api_key)],
)
async def positions(
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[PositionOut]:
    quotes = await dashboard.positions()
    return [
        PositionOut(
            position_id=q.position.position_id,
            symbol=q.position.symbol,
            quantity=q.position.quantity,
            avg_entry_price=str(q.position.avg_entry_price.amount),
            current_price=str(q.current_price) if q.current_price is not None else None,
            unrealized_pnl=str(q.unrealized_pnl) if q.unrealized_pnl is not None else None,
            status=q.position.status.value,
            stop_loss=(
                str(q.position.stop_loss.amount) if q.position.stop_loss is not None else None
            ),
            take_profit=(
                str(q.position.take_profit.amount) if q.position.take_profit is not None else None
            ),
            realized_pnl=(
                str(q.position.realized_pnl.amount) if q.position.realized_pnl is not None else None
            ),
            opened_at=q.position.opened_at,
            closed_at=q.position.closed_at,
        )
        for q in quotes
    ]


@router.get(
    "/agents",
    response_model=list[AgentMetricOut],
    dependencies=[Depends(require_api_key)],
)
async def agents(
    limit: int = 50,
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[AgentMetricOut]:
    metrics = await dashboard.agents(limit=limit)
    return [
        AgentMetricOut(
            agent_name=m.agent_name,
            metric_name=m.metric_name,
            value=str(m.value),
            window=m.window,
            computed_at=m.computed_at,
        )
        for m in metrics
    ]


@router.get(
    "/allocation",
    response_model=list[AllocationOut],
    dependencies=[Depends(require_api_key)],
)
async def allocation(
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[AllocationOut]:
    slices = await dashboard.allocation()
    return [
        AllocationOut(
            symbol=s.symbol,
            sector=s.sector,
            market_value=str(s.market_value),
            weight_pct=s.weight_pct,
        )
        for s in slices
    ]


@router.get(
    "/top-stocks",
    response_model=list[TopStockOut],
    dependencies=[Depends(require_api_key)],
)
async def top_stocks(
    metric: str = "overall",
    limit: int = 20,
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[TopStockOut]:
    ranking = await dashboard.top_stocks(metric, limit)
    return [TopStockOut(symbol=s, score=v) for s, v in ranking]


@router.get(
    "/trades",
    response_model=list[TradeOut],
    dependencies=[Depends(require_api_key)],
)
async def trades(
    from_: datetime | None = None,
    limit: int = 100,
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[TradeOut]:
    records = await dashboard.trades(since=from_, limit=limit)
    return [
        TradeOut(
            trade_id=t.trade_id,
            symbol=t.symbol,
            strategy=t.strategy,
            side=t.side.value,
            quantity=str(t.quantity),
            entry_price=str(t.entry_price),
            exit_price=str(t.exit_price),
            pnl=str(t.pnl) if t.pnl is not None else None,
            pnl_pct=str(t.pnl_pct) if t.pnl_pct is not None else None,
            fees=str(t.fees),
            entry_time=t.entry_time,
            exit_time=t.exit_time,
            outcome=t.outcome,
            mode=t.mode.value,
        )
        for t in records
    ]


@router.get(
    "/risk",
    response_model=list[RiskAssessmentOut],
    dependencies=[Depends(require_api_key)],
)
async def risk(
    limit: int = 50,
    risk_repo: RiskRepository = Depends(get_risk_repository),
) -> list[RiskAssessmentOut]:
    assessments = await risk_repo.recent(limit)
    return [
        RiskAssessmentOut(
            symbol=a.symbol,
            approved=a.approved,
            rejection_reasons=a.rejection_reasons,
            position_size=str(a.position_size) if a.position_size is not None else None,
            stop_loss=str(a.stop_loss) if a.stop_loss is not None else None,
            take_profit=str(a.take_profit) if a.take_profit is not None else None,
            exposure_pct=str(a.exposure_pct) if a.exposure_pct is not None else None,
            created_at=a.created_at,
        )
        for a in assessments
    ]


@router.get(
    "/logs",
    response_model=list[LogOut],
    dependencies=[Depends(require_api_key)],
)
async def logs(
    level: str | None = None,
    component: str | None = None,
    limit: int = 50,
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[LogOut]:
    entries = await dashboard.logs(level=level, component=component, limit=limit)
    return [
        LogOut(
            level=e.level,
            message=e.message,
            component=e.component,
            context=e.context,
            created_at=e.created_at,
        )
        for e in entries
    ]


@router.get(
    "/performance",
    response_model=list[PerformanceSummaryOut],
    dependencies=[Depends(require_api_key)],
)
async def performance(
    strategy: str | None = None,
    mode: str | None = None,
    limit: int = 50,
    dashboard: DashboardService = Depends(get_dashboard_service),
) -> list[PerformanceSummaryOut]:
    from qtrader.domain.value_objects import TradingMode

    selected_mode = TradingMode(mode) if mode else None
    records = await dashboard.performance(strategy=strategy, mode=selected_mode, limit=limit)
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
        for p in records
    ]
