"""Backtest submission & history router (Phase 6 engine, Phase 7 API)."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException

from qtrader.application.services.backtest import BacktestParams, BacktestRunner
from qtrader.domain.entities import BacktestRun
from qtrader.domain.ports import BacktestRepository
from qtrader.domain.value_objects import Interval
from qtrader.interfaces.api.dependencies import (
    get_backtest_repository,
    get_backtest_runner,
    require_api_key,
)
from qtrader.interfaces.api.schemas import (
    BacktestCompare,
    BacktestRunOut,
    BacktestSubmit,
    PerformanceSummaryOut,
)

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])


def _run_out(run: BacktestRun) -> BacktestRunOut:
    metrics = None
    if run.metrics is not None:
        m = run.metrics
        metrics = PerformanceSummaryOut(
            strategy=m.strategy,
            mode=m.mode.value,
            period_start=m.period_start,
            period_end=m.period_end,
            total_return=str(m.total_return) if m.total_return is not None else None,
            sharpe=str(m.sharpe) if m.sharpe is not None else None,
            sortino=str(m.sortino) if m.sortino is not None else None,
            max_drawdown=str(m.max_drawdown) if m.max_drawdown is not None else None,
            win_rate=str(m.win_rate) if m.win_rate is not None else None,
            profit_factor=str(m.profit_factor) if m.profit_factor is not None else None,
            trades_count=m.trades_count,
            final_equity=str(m.final_equity) if m.final_equity is not None else None,
        )
    return BacktestRunOut(
        run_id=run.run_id,
        name=run.name,
        universe=run.universe,
        start=run.start,
        end=run.end,
        initial_capital=str(run.initial_capital.amount),
        interval=run.interval.value,
        strategy=run.strategy,
        commission_bps=str(run.commission_bps),
        slippage_bps=str(run.slippage_bps),
        final_capital=str(run.final_capital.amount) if run.final_capital is not None else None,
        status=run.status,
        created_at=run.created_at,
        metrics=metrics,
    )


@router.post(
    "",
    response_model=BacktestRunOut,
    dependencies=[Depends(require_api_key)],
)
async def submit_backtest(
    body: BacktestSubmit,
    runner: BacktestRunner = Depends(get_backtest_runner),
) -> BacktestRunOut:
    symbols = [s.strip().upper() for s in body.symbols if s.strip()]
    if not symbols:
        raise HTTPException(status_code=422, detail="symbols must not be empty")
    try:
        result = await runner.run(
            name=body.name,
            symbols=symbols,
            start=body.start,
            end=body.end,
            initial_capital=Decimal(body.initial_capital),
            params=BacktestParams(
                interval=Interval(body.interval),
                strategy=body.strategy,
                commission_bps=body.commission_bps,
                slippage_bps=body.slippage_bps,
                warmup_bars=body.warmup_bars,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _run_out(result.run)


@router.get(
    "",
    response_model=list[BacktestRunOut],
    dependencies=[Depends(require_api_key)],
)
async def list_backtests(
    name: str | None = None,
    limit: int = 20,
    repo: BacktestRepository = Depends(get_backtest_repository),
) -> list[BacktestRunOut]:
    runs = await repo.latest(name=name, limit=limit)
    return [_run_out(r) for r in runs]


@router.get(
    "/{run_id}",
    response_model=BacktestRunOut,
    dependencies=[Depends(require_api_key)],
)
async def get_backtest(
    run_id: int,
    repo: BacktestRepository = Depends(get_backtest_repository),
) -> BacktestRunOut:
    run = await repo.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backtest run not found")
    return _run_out(run)


@router.post(
    "/{run_id}/compare",
    response_model=list[BacktestRunOut],
    dependencies=[Depends(require_api_key)],
)
async def compare_backtest(
    run_id: int,
    body: BacktestCompare,
    repo: BacktestRepository = Depends(get_backtest_repository),
) -> list[BacktestRunOut]:
    first = await repo.get(run_id)
    second = await repo.get(body.other_run_id)
    if first is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    if second is None:
        raise HTTPException(status_code=404, detail=f"run {body.other_run_id} not found")
    return [_run_out(first), _run_out(second)]
