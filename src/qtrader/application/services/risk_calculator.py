"""Risk Calculator — the pure, unit-testable risk engine behind the Risk Agent.

Every limit is explicit (a frozen ``RiskPolicy``); inputs arrive as plain
numbers so the calculator has no I/O and no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from qtrader.domain.entities import RiskAssessment
from qtrader.domain.value_objects import Decision

_QTY_QUANT = Decimal("0.0001")
_PRICE_QUANT = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    risk_per_trade_pct: float = 0.01
    max_daily_loss_pct: float = 0.03
    max_portfolio_exposure_pct: float = 0.8
    max_positions: int = 10
    per_sector_limit_pct: float = 0.4
    max_position_pct_adv: float = 0.01
    min_cooldown_minutes: int = 5
    max_trades_per_day: int = 10
    atr_stop_mult: float = 1.5
    take_profit_r_mult: float = 2.0
    allow_add_to_position: bool = False


@dataclass(frozen=True, slots=True)
class RiskInputs:
    """Everything the gate needs to know about the current state."""

    decision: Decision
    symbol: str
    entry_price: Decimal
    atr: Decimal | None
    equity: Decimal
    current_exposure_pct: float
    open_positions: int
    sector_exposure_pct: float
    adv_daily: Decimal | None
    cooldown_remaining_minutes: float
    daily_pnl_pct: float
    trades_today: int
    position_quantity: Decimal | None = None
    position_stop: Decimal | None = None
    atr_stop_distance: Decimal | None = None


def _dec(value: Decimal | float, quant: Decimal = _PRICE_QUANT) -> Decimal:
    return Decimal(str(value)).quantize(quant)


class RiskCalculator:
    def __init__(self, policy: RiskPolicy) -> None:
        self._policy = policy

    def assess(self, inputs: RiskInputs) -> RiskAssessment:
        reasons: list[str] = []
        atr = inputs.atr or inputs.entry_price * Decimal("0.02")
        atr_stop = (
            inputs.atr_stop_distance
            if inputs.atr_stop_distance is not None
            else atr * Decimal(str(self._policy.atr_stop_mult))
        )

        stop_loss = _dec(inputs.entry_price - atr_stop)
        take_profit = _dec(
            inputs.entry_price + atr_stop * Decimal(str(self._policy.take_profit_r_mult))
        )
        risk_per_trade = Decimal(str(self._policy.risk_per_trade_pct))
        risk_dollar = inputs.equity * risk_per_trade
        position_size = (risk_dollar / atr_stop) if atr_stop else Decimal(0)
        position_size = position_size.quantize(_QTY_QUANT)

        metadata: dict[str, object] = {
            "atr": float(atr),
            "atr_stop_distance": float(atr_stop),
            "entry_price": float(inputs.entry_price),
        }

        # SELL = close an existing position (no new sizing).
        if inputs.decision is Decision.SELL:
            if not inputs.position_quantity:
                reasons.append("no open position to close")
            else:
                position_size = Decimal(inputs.position_quantity)
                if inputs.position_stop:
                    stop_loss = _dec(inputs.position_stop)

        # BUY = open a new position only: never re-buy a symbol we already hold
        # unless the policy explicitly allows adding to a position.
        if (
            inputs.decision is Decision.BUY
            and inputs.position_quantity
            and not self._policy.allow_add_to_position
        ):
            reasons.append(f"position already open for {inputs.symbol}")

        if inputs.open_positions >= self._policy.max_positions:
            reasons.append(
                f"max positions reached ({inputs.open_positions}/{self._policy.max_positions})"
            )
        projected = (
            inputs.current_exposure_pct
            + float(position_size * inputs.entry_price / inputs.equity)
            if inputs.equity
            else 1.0
        )
        if projected > self._policy.max_portfolio_exposure_pct:
            reasons.append(
                f"projected exposure {projected * 100:.1f}% > "
                f"limit {self._policy.max_portfolio_exposure_pct * 100:.1f}%"
            )
        if inputs.sector_exposure_pct > self._policy.per_sector_limit_pct:
            reasons.append(
                f"sector exposure {inputs.sector_exposure_pct * 100:.1f}% > "
                f"limit {self._policy.per_sector_limit_pct * 100:.1f}%"
            )
        if (
            inputs.adv_daily is not None
            and inputs.adv_daily > 0
            and (position_size * inputs.entry_price) / inputs.adv_daily
            > self._policy.max_position_pct_adv
        ):
            reasons.append("position too large relative to ADV (liquidity)")
        if inputs.cooldown_remaining_minutes > 0:
            reasons.append(
                f"cooldown active ({inputs.cooldown_remaining_minutes:.0f}m remaining)"
            )
        if inputs.trades_today >= self._policy.max_trades_per_day:
            reasons.append(
                f"max trades per day reached "
                f"({inputs.trades_today}/{self._policy.max_trades_per_day})"
            )
        if inputs.daily_pnl_pct < -self._policy.max_daily_loss_pct:
            reasons.append(
                f"daily loss {inputs.daily_pnl_pct * 100:.1f}% exceeds "
                f"{self._policy.max_daily_loss_pct * 100:.1f}% limit"
            )
        if position_size <= 0:
            reasons.append("position size is zero")

        approved = not reasons
        return RiskAssessment(
            decision_uuid="",
            symbol=inputs.symbol,
            approved=approved,
            rejection_reasons=reasons,
            position_size=position_size if approved else None,
            stop_loss=stop_loss if approved else None,
            take_profit=take_profit if approved else None,
            risk_per_trade_pct=risk_per_trade if approved else None,
            exposure_pct=Decimal(str(round(projected, 6))) if approved else None,
            max_daily_loss_pct=Decimal(str(self._policy.max_daily_loss_pct)),
            daily_pnl_pct=Decimal(str(round(inputs.daily_pnl_pct, 6))),
            metadata=metadata,
        )

    @property
    def policy(self) -> RiskPolicy:
        return self._policy

    def with_policy(self, **overrides: Any) -> RiskCalculator:
        return RiskCalculator(replace(self._policy, **overrides))
