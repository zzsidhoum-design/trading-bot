"""The central Risk Engine — the authoritative gate between strategies and
execution.

The engine is independent of every AI agent and trading strategy. A proposed
trade enters as a plain :class:`ProposedTrade` record (an input, never a
decision-maker) and comes out as an approved, modified or rejected
:class:`GateDecision`. No agent or strategy may bypass position limits,
exposure limits, drawdown limits, data-quality checks, execution constraints
or the kill switch.

Evaluation order (highest authority first):

1. kill switch — tripped means every trade is rejected;
2. strategy control status — SUSPENDED rejects, REDUCED halves (configurable);
3. data quality — missing/invalid reference price or sizing inputs reject;
4. drawdown protection — portfolio drawdown / daily loss / consecutive losses;
5. position sizing + portfolio constraints (approve / cap / reject);
6. execution constraints (Phase 4 liquidity/impact assumptions) cap the order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

from qtrader.application.execution.liquidity import LiquidityModel
from qtrader.application.execution.models import LiquidityAssessment
from qtrader.application.portfolio_mgmt.constraints import ConstraintEngine
from qtrader.application.portfolio_mgmt.correlation import CorrelationProvider
from qtrader.application.portfolio_mgmt.drawdown import DrawdownGuard, KillSwitch
from qtrader.application.portfolio_mgmt.models import (
    DrawdownProtection,
    GateDecision,
    GateVerdict,
    PortfolioConstraints,
    PortfolioSnapshot,
    PositionSize,
    PositionSizingMethod,
    ProposedTrade,
    SizingPolicy,
    StrategyControlState,
    StrategyControlStatus,
    quantize_qty,
)
from qtrader.application.portfolio_mgmt.sizing import (
    SizeInput,
    apply_control_weights,
    sizer_for,
)
from qtrader.domain.value_objects import PriceBar, TradeSide

_QTY_QUANT = Decimal("0.0001")

# Execution-liquidity gate (Phase 4 assumptions). A callable that returns a
# LiquidityAssessment for (symbol, price, notional); the engine uses it only
# to cap (never to add risk), and skips it when no provider is wired.
LiquidityChecker = Callable[[str, Decimal, Decimal], object]


class PortfolioRiskEngine:
    """Central, authoritative portfolio & risk gate."""

    def __init__(
        self,
        *,
        constraints: PortfolioConstraints,
        drawdown_protection: DrawdownProtection,
        sizing_policy: SizingPolicy,
        kill_switch: KillSwitch,
        correlation_provider: CorrelationProvider | None = None,
        liquidity_checker: LiquidityChecker | None = None,
        control_states: Mapping[str, StrategyControlState] | None = None,
    ) -> None:
        self._constraints = constraints
        self._protection = drawdown_protection
        self._sizing_policy = sizing_policy
        self._kill_switch = kill_switch
        self._correlation_provider = correlation_provider
        self._liquidity_checker = liquidity_checker
        self._control_states = dict(control_states or {})
        self._constraint_engine = ConstraintEngine(constraints)
        self._drawdown_guard = DrawdownGuard(drawdown_protection)
        self._sizer = sizer_for(sizing_policy.method)

    @property
    def constraints(self) -> PortfolioConstraints:
        return self._constraints

    @property
    def sizing_policy(self) -> SizingPolicy:
        return self._sizing_policy

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    @property
    def control_states(self) -> Mapping[str, StrategyControlState]:
        return dict(self._control_states)

    def update_control_state(self, state: StrategyControlState) -> None:
        self._control_states[state.strategy_id] = state

    def gate(self, trade: ProposedTrade, snapshot: PortfolioSnapshot) -> GateDecision:
        # 1. Kill switch is the highest authority.
        if self._kill_switch.is_tripped:
            return self._reject(
                trade,
                ("KILL SWITCH TRIPPED: all trading halted by emergency shutdown",),
            )

        # 2. Strategy control status.
        state = self._control_states.get(trade.strategy_id)
        if state is not None and state.status is StrategyControlStatus.SUSPENDED:
            return self._reject(
                trade,
                (
                    f"strategy {trade.strategy_id} is SUSPENDED; "
                    "trading blocked by failure controls",
                ),
            )
        weight_factor = state.weight_factor(self._protection) if state is not None else 1.0

        # 3. Data quality checks (authoritative; no AI can bypass them).
        data_issues = self._data_quality_issues(trade)
        if data_issues:
            return self._reject(trade, tuple(data_issues))

        # 4. Drawdown protection.
        drawdown_breaches = self._drawdown_guard.portfolio_breaches(snapshot)
        if drawdown_breaches:
            return self._reject(trade, tuple(drawdown_breaches))

        # 5. Size + constraints.
        size = self._sizer.size(
            SizeInput(
                trade=trade,
                snapshot=snapshot,
                policy=self._sizing_policy,
                constraints=self._constraints,
            )
        )
        if size.quantity <= 0:
            return self._reject(trade, ("position size is zero after sizing",))

        verdict = self._constraint_engine.evaluate(
            snapshot,
            trade,
            size,
            correlation_provider=self._correlation_provider,
        )
        reasons: list[str] = []
        warnings: list[str] = list(verdict.warnings)
        modifications: list[str] = []
        approved_quantity = size.quantity

        if not verdict.approved:
            reasons.extend(verdict.violations)
            return GateDecision(
                verdict=GateVerdict.REJECT,
                reasons=tuple(reasons),
                warnings=tuple(warnings),
                position_size=size,
                evaluated_exposure_pct=verdict.evaluated_exposure_pct,
            )

        # Apply a constraint-driven cap.
        if verdict.cap_quantity is not None and verdict.cap_quantity < approved_quantity:
            approved_quantity = verdict.cap_quantity
            modifications.append(f"size capped to {approved_quantity} shares")
            warnings.append("trade modified by portfolio constraints")

        # 6. Execution-liquidity cap (Phase 4 assumptions; optional).
        if self._liquidity_checker is not None:
            notional = approved_quantity * trade.reference_price
            assessment = self._liquidity_checker(trade.symbol, trade.reference_price, notional)
            max_fillable = _max_fillable(assessment)
            if max_fillable is not None and max_fillable < approved_quantity:
                approved_quantity = max_fillable
                modifications.append(
                    f"size capped to {approved_quantity} shares by execution liquidity"
                )
                warnings.append("trade modified by execution constraints")

        if weight_factor < 1.0:
            approved_quantity = apply_control_weights(
                approved_quantity,
                trade.strategy_id,
                weight_factor,
            )
            modifications.append(
                f"strategy status weight factor {weight_factor:.2f} applied"
            )
            warnings.append("trade modified by strategy status controls")

        approved_quantity = quantize_qty(approved_quantity)
        if approved_quantity <= 0:
            reasons.append("approved size is zero after constraints")
            return GateDecision(
                verdict=GateVerdict.REJECT,
                reasons=tuple(reasons),
                warnings=tuple(warnings),
                modifications=tuple(modifications),
                position_size=size,
                evaluated_exposure_pct=verdict.evaluated_exposure_pct,
            )

        capped_weight = (
            float(approved_quantity * trade.reference_price / snapshot.equity)
            if snapshot.equity
            else 0.0
        )
        final_size = PositionSize(
            symbol=trade.symbol,
            quantity=approved_quantity,
            notional=(approved_quantity * trade.reference_price).quantize(_QTY_QUANT),
            weight_pct=capped_weight,
            method=self._sizing_policy.method,
            warnings=size.warnings + tuple(warnings),
        )

        if approved_quantity < size.quantity or modifications:
            return GateDecision(
                verdict=GateVerdict.MODIFY,
                reasons=tuple(reasons),
                warnings=final_size.warnings,
                approved_quantity=approved_quantity,
                modifications=tuple(modifications),
                position_size=final_size,
                evaluated_exposure_pct=verdict.evaluated_exposure_pct,
            )
        return GateDecision(
            verdict=GateVerdict.APPROVE,
            warnings=final_size.warnings,
            approved_quantity=approved_quantity,
            position_size=final_size,
            evaluated_exposure_pct=verdict.evaluated_exposure_pct,
        )

    def set_sizing_method(self, method: PositionSizingMethod) -> None:
        from dataclasses import replace

        self._sizing_policy = replace(self._sizing_policy, method=method)
        self._sizer = sizer_for(method)

    def _data_quality_issues(self, trade: ProposedTrade) -> list[str]:
        issues: list[str] = []
        if trade.reference_price is None or trade.reference_price <= 0:
            issues.append(f"data quality failure: no valid reference price for {trade.symbol}")
        if trade.quantity is None or trade.quantity <= 0:
            issues.append(f"data quality failure: non-positive quantity for {trade.symbol}")
        if (
            self._sizing_policy.method is PositionSizingMethod.RISK_BUDGET
            and trade.atr_pct is None
            and trade.side is TradeSide.BUY
        ):
            issues.append(
                "data quality failure: missing ATR for risk-budget sizing "
                f"of {trade.symbol}"
            )
        return issues

    def _reject(self, trade: ProposedTrade, reasons: tuple[str, ...]) -> GateDecision:
        return GateDecision(verdict=GateVerdict.REJECT, reasons=reasons)


def _max_fillable(assessment: object) -> Decimal | None:
    """Read ``max_fillable`` off a Phase 4 LiquidityAssessment (duck-typed so
    the engine never depends on the execution package at import time)."""
    value = getattr(assessment, "max_fillable", None)
    if value is None:
        return None
    return Decimal(str(value))


def make_liquidity_checker(
    liquidity: LiquidityModel,
    bars_by_symbol: Mapping[str, Sequence[PriceBar]] | None = None,
) -> LiquidityChecker:
    """Build a liquidity checker from a Phase 4 :class:`LiquidityModel`.

    For each symbol the checker estimates ADV from the provided bars and runs
    the model's submit-time liquidity gate (volume/dollar-volume floors and
    the notional-vs-ADV budget). The engine uses it only to cap sizes. The
    max-fillable quantity is derived from the ADV-dollar budget and the
    reference price so the engine can cap orders that would exceed a fraction
    of the symbol's daily dollar volume.
    """
    bars_by_symbol = bars_by_symbol or {}

    def check(symbol: str, price: Decimal, notional: Decimal) -> object:
        bars = bars_by_symbol.get(symbol, ())
        adv_volume, adv_dollar = liquidity.adv_for(list(bars))
        assessment = liquidity.check_size(
            order_notional=notional,
            adv_volume=adv_volume,
            adv_dollar=adv_dollar,
        )
        max_notional = adv_dollar * Decimal(str(liquidity.assumptions.max_notional_pct_adv))
        max_fillable = int(max_notional / price) if price > 0 else 0
        return LiquidityAssessment(
            approved=assessment.approved,
            reasons=assessment.reasons,
            max_fillable=max_fillable,
        )

    return check


__all__ = ["LiquidityChecker", "PortfolioRiskEngine", "make_liquidity_checker"]
