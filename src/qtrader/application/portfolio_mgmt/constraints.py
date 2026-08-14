"""Portfolio constraints — hard limits no AI/strategy layer may bypass.

The engine evaluates a proposed trade against configurable limits:

* maximum position size
* maximum portfolio exposure
* maximum sector exposure
* maximum correlated exposure
* maximum number of simultaneous positions
* maximum turnover
* maximum leverage

Each check returns a structured result so callers can distinguish an
approvable-and-capable case (MODIFY) from a hard violation (REJECT).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from qtrader.application.portfolio_mgmt.correlation import (
    CorrelationProvider,
    proposed_correlated_exposure,
    proposed_sector_exposure,
)
from qtrader.application.portfolio_mgmt.models import (
    PortfolioConstraints,
    PortfolioSnapshot,
    PositionSize,
    ProposedTrade,
)

_QTY_QUANT = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ConstraintVerdict:
    """Aggregate result of all constraint checks for one proposed trade."""

    approved: bool
    cap_quantity: Decimal | None
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evaluated_exposure_pct: float | None = None


class ConstraintEngine:
    """Evaluates portfolio constraints for a proposed trade + size."""

    def __init__(self, constraints: PortfolioConstraints) -> None:
        self._constraints = constraints

    @property
    def constraints(self) -> PortfolioConstraints:
        return self._constraints

    def evaluate(
        self,
        snapshot: PortfolioSnapshot,
        trade: ProposedTrade,
        size: PositionSize,
        correlation_provider: CorrelationProvider | None = None,
    ) -> ConstraintVerdict:
        violations: list[str] = []
        warnings: list[str] = []
        constraints = self._constraints
        equity = snapshot.equity
        if equity <= 0:
            return ConstraintVerdict(
                approved=False,
                cap_quantity=None,
                violations=("portfolio equity is zero or negative",),
            )

        projected_exposure = snapshot.gross_exposure_pct + size.weight_pct
        # Leverage is gross exposure beyond 100% of equity (long-only, cash
        # book -> 0). The snapshot's leverage_pct is informational; the hard
        # limit is derived from projected gross exposure.
        projected_leverage = max(0.0, projected_exposure - 1.0)

        cap = size.quantity

        # 1. Maximum position size.
        position_breach = size.weight_pct > constraints.max_position_weight_pct + 1e-9
        if position_breach:
            violations.append(
                f"position size {size.weight_pct * 100:.1f}% exceeds "
                f"max {constraints.max_position_weight_pct * 100:.1f}%"
            )
        # 2. Maximum portfolio exposure.
        if projected_exposure > constraints.max_portfolio_exposure_pct + 1e-9:
            violations.append(
                f"projected exposure {projected_exposure * 100:.1f}% exceeds "
                f"limit {constraints.max_portfolio_exposure_pct * 100:.1f}%"
            )
        # 3. Maximum sector exposure.
        if trade.sector is not None:
            exposures = proposed_sector_exposure(snapshot, trade, size)
            sector = trade.sector or "unknown"
            sector_pct = exposures.get(sector, 0.0)
            if sector_pct > constraints.max_sector_exposure_pct + 1e-9:
                violations.append(
                    f"sector exposure {sector_pct * 100:.1f}% exceeds "
                    f"limit {constraints.max_sector_exposure_pct * 100:.1f}%"
                )
        # 4. Maximum correlated exposure.
        if correlation_provider is not None:
            correlated = proposed_correlated_exposure(
                snapshot,
                trade,
                size,
                correlation_provider,
                constraints.correlation_threshold,
            )
            if correlated > constraints.max_correlated_exposure_pct + 1e-9:
                violations.append(
                    f"correlated exposure {correlated * 100:.1f}% exceeds "
                    f"limit {constraints.max_correlated_exposure_pct * 100:.1f}%"
                )
        # 5. Maximum simultaneous positions.
        if snapshot.positions_count >= constraints.max_positions:
            violations.append(
                f"max positions reached "
                f"({snapshot.positions_count}/{constraints.max_positions})"
            )
        # 6. Maximum turnover (projected as position weight on top of current).
        projected_turnover = snapshot.turnover_30d_pct + size.weight_pct
        if projected_turnover > constraints.max_turnover_pct + 1e-9:
            violations.append(
                f"projected turnover {projected_turnover * 100:.1f}% exceeds "
                f"limit {constraints.max_turnover_pct * 100:.1f}%"
            )
        # 7. Maximum leverage (gross exposure beyond 100% of equity).
        leverage_breach = projected_leverage > constraints.max_leverage_pct + 1e-9
        if leverage_breach:
            violations.append(
                f"projected leverage {projected_leverage * 100:.1f}% exceeds "
                f"limit {constraints.max_leverage_pct * 100:.1f}%"
            )

        # Capping: if only the position-size/leverage cap is breached AND there
        # is remaining budget to absorb the new trade, we can modify the order
        # to fit. Hard violations (positions, sector, correlated exposure,
        # turnover, portfolio exposure) cannot be fixed by shrinking the new
        # trade alone; nor can a leverage breach with zero remaining budget.
        remaining_leverage = constraints.max_leverage_pct - snapshot.leverage_pct
        hard_blocks = [
            v
            for v in violations
            if not (
                v.startswith("position size")
                or (v.startswith("projected leverage") and remaining_leverage > 0.0)
            )
        ]
        if hard_blocks:
            return ConstraintVerdict(
                approved=False,
                cap_quantity=None,
                violations=tuple(violations),
                warnings=tuple(warnings),
                evaluated_exposure_pct=projected_exposure,
            )
        if position_breach or (leverage_breach and remaining_leverage > 0.0):
            cap_weight = constraints.max_position_weight_pct
            if leverage_breach:
                cap_weight = min(cap_weight, max(0.0, remaining_leverage))
            capped_notional = equity * Decimal(str(cap_weight))
            cap = (
                capped_notional / trade.reference_price
                if trade.reference_price > 0
                else Decimal(0)
            )
            cap = cap.quantize(_QTY_QUANT)
            warnings.append(
                f"position capped to {cap_weight * 100:.1f}% of equity"
            )
        return ConstraintVerdict(
            approved=True,
            cap_quantity=cap,
            violations=tuple(violations),
            warnings=tuple(warnings),
            evaluated_exposure_pct=projected_exposure,
        )


__all__ = ["ConstraintEngine", "ConstraintVerdict"]
