"""Portfolio & orders router (read side for now; writes go through use cases later)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from qtrader.domain.ports import PortfolioRepository
from qtrader.interfaces.api.dependencies import get_portfolio_repository, require_api_key
from qtrader.interfaces.api.schemas import PortfolioSummary

router = APIRouter(prefix="/api/v1/portfolio", tags=["portfolio"])


@router.get(
    "",
    response_model=PortfolioSummary,
    dependencies=[Depends(require_api_key)],
)
async def portfolio_summary(
    portfolio_id: int = 1,
    portfolio_repo: PortfolioRepository = Depends(get_portfolio_repository),
) -> PortfolioSummary:
    portfolio = await portfolio_repo.get(portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="portfolio not found")
    return PortfolioSummary(
        name=portfolio.name,
        currency=portfolio.currency,
        mode=portfolio.mode,
        status=portfolio.status,
        initial_capital=str(portfolio.initial_capital.amount),
        current_cash=str(portfolio.current_cash.amount),
    )
