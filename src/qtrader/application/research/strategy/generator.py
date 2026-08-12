"""Strategy generator — bounded, constrained hypothesis synthesis.

Hypotheses are assembled from the *validated* feature families (trend entry,
momentum entry, volume filter, volatility filter, regime gate, exit rule).
Every candidate is checked against the search limits and rejected with a reason
when it is meaningless, redundant or excessively complex. A generated strategy
is a hypothesis only — profitability is never assumed until it passes the
research workflow's net-of-cost stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qtrader.application.research.strategy.feature_library import FeatureLibrary
from qtrader.application.research.strategy.specs import (
    Condition,
    EntryRule,
    ExitRule,
    Operator,
    RegimeFilter,
    StrategySpec,
)
from qtrader.domain.value_objects import Interval

# Curated, validated building blocks (no blind indicator soup).
# (feature, operator, target) where target is a float threshold or a feature name.
TREND_ENTRIES: tuple[tuple[tuple[str, str, Any], ...], ...] = (
    (("close", ">", "ema_21"),),
    (("ema_9", ">", "ema_21"),),
    (("ema_21", ">", "sma_50"),),
)
MOMENTUM_ENTRIES: tuple[tuple[tuple[str, str, Any], ...], ...] = (
    (("rsi", ">", 50.0),),
    (("macd", ">", "macd_signal"),),
)
ENTRY_BASES: tuple[tuple[tuple[str, str, Any], ...], ...] = TREND_ENTRIES + MOMENTUM_ENTRIES
FILTER_COMBOS: tuple[tuple[tuple[str, str, Any], ...], ...] = (
    (),
    (("volume_ratio", ">", 1.5),),
    (("atr_pct", "<", 0.05),),
    (("volume_ratio", ">", 1.5), ("atr_pct", "<", 0.05)),
)
EXIT_RULES: tuple[tuple[tuple[str, str, Any], ...], ...] = (
    (("rsi", ">", 70.0),),
    (("macd_hist", "<", 0.0),),
    (("close", "<", "boll_middle"),),
)
REGIME_GATE: tuple[tuple[str, str, Any], ...] = (("close", ">", "sma_200"),)

NARROW_PARAM_EPSILON = 2.0


@dataclass(frozen=True, slots=True)
class SearchLimits:
    """Search-space controls: caps, param ranges and the compute budget."""

    max_strategies: int = 60
    max_indicators: int = 5
    max_conditions: int = 3
    max_exit_conditions: int = 2
    max_complexity: int = 8
    computational_budget: int = 60
    intervals: tuple[Interval, ...] = (Interval.D1,)
    regime_gate: bool = True
    allow_momentum_entries: bool = True

    def __post_init__(self) -> None:
        if self.max_strategies < 1 or self.computational_budget < 1:
            raise ValueError("search limits must allow at least one strategy")


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Outcome of one generation pass (specs + rejection ledger)."""

    specs: tuple[StrategySpec, ...] = ()
    rejections: dict[str, str] = field(default_factory=dict)
    candidates_considered: int = 0

    @property
    def rejected_count(self) -> int:
        return len(self.rejections)


def _to_condition(item: tuple[str, str, Any]) -> Condition:
    feature, op_text, target = item
    op = Operator(op_text)
    if isinstance(target, str):
        return Condition(feature=feature, op=op, ref_feature=target)
    return Condition(feature=feature, op=op, value=float(target))


def _describe(conditions: tuple[Condition, ...]) -> str:
    return ", ".join(
        c.ref_feature if c.ref_feature is not None else f"{c.feature} {c.op} {c.value:g}"
        for c in conditions
    )


class StrategyGenerator:
    """Builds bounded, deterministic, constraint-checked strategy hypotheses."""

    def __init__(self, library: FeatureLibrary | None = None) -> None:
        self._library = library or FeatureLibrary()

    def generate(self, limits: SearchLimits) -> GenerationResult:
        candidates: list[StrategySpec] = []
        rejections: dict[str, str] = {}
        namespace = "strat"
        seq = 0

        bases: tuple[tuple[tuple[str, str, Any], ...], ...] = ENTRY_BASES
        if not limits.allow_momentum_entries:
            bases = TREND_ENTRIES

        for base in bases:
            for filters in FILTER_COMBOS:
                for exit_rule in EXIT_RULES:
                    if len(candidates) + len(rejections) >= limits.max_strategies + 64:
                        break
                    candidate = self._build_candidate(
                        base, filters, exit_rule, limits, namespace, seq
                    )
                    seq += 1
                    if candidate is None:
                        continue
                    rejection = self._constraint_violation(candidate, limits)
                    if rejection is not None:
                        rejections[candidate.id] = rejection
                        continue
                    candidates.append(candidate)
                    if len(candidates) >= limits.max_strategies:
                        break
                if len(candidates) >= limits.max_strategies:
                    break
            if len(candidates) >= limits.max_strategies:
                break

        return GenerationResult(
            specs=tuple(candidates),
            rejections=rejections,
            candidates_considered=seq,
        )

    def _build_candidate(
        self,
        base: tuple[tuple[str, str, Any], ...],
        filters: tuple[tuple[str, str, Any], ...],
        exit_rule: tuple[tuple[str, str, Any], ...],
        limits: SearchLimits,
        namespace: str,
        seq: int,
    ) -> StrategySpec | None:
        entry_conditions = tuple(_to_condition(item) for item in base)
        filter_conditions = tuple(_to_condition(item) for item in filters)
        entry = EntryRule(conditions=entry_conditions + filter_conditions, logic="all")
        exit_rule_obj = ExitRule(
            conditions=tuple(_to_condition(item) for item in exit_rule), logic="any"
        )
        regime: RegimeFilter | None = None
        if limits.regime_gate and seq % 2 == 1:
            regime = RegimeFilter(conditions=tuple(_to_condition(item) for item in REGIME_GATE))

        features: list[str] = []
        for condition in entry.conditions + exit_rule_obj.conditions + (
            regime.conditions if regime else ()
        ):
            if condition.feature not in features:
                features.append(condition.feature)
            if condition.ref_feature and condition.ref_feature not in features:
                features.append(condition.ref_feature)

        params: dict[str, Any] = {}
        for condition in entry.conditions + exit_rule_obj.conditions:
            if condition.ref_feature is None and condition.feature not in params:
                params[condition.feature] = condition.value

        complexity = (
            len(entry.conditions)
            + len(exit_rule_obj.conditions)
            + len(regime.conditions if regime else ())
            + len(params)
        )
        direction = "long"
        description = (
            f"long {_describe(entry.conditions)}"
            + (" under regime" if regime else "")
            + f"; exit on {_describe(exit_rule_obj.conditions)}"
        )
        return StrategySpec(
            id=f"{namespace}-{seq:04d}",
            name=f"auto-{seq:04d}",
            version=1,
            direction=direction,
            entry=entry,
            exit=exit_rule_obj,
            regime=regime,
            timeframes=limits.intervals,
            params=params,
            features=tuple(features),
            complexity=complexity,
            description=description,
        )

    def _constraint_violation(
        self, spec: StrategySpec, limits: SearchLimits
    ) -> str | None:
        all_conditions = list(spec.entry.conditions) + list(spec.exit.conditions)
        if spec.regime is not None:
            all_conditions += list(spec.regime.conditions)

        used: set[str] = set()
        for condition in all_conditions:
            if condition.feature != "close":
                used.add(condition.feature)
            if condition.ref_feature:
                used.add(condition.ref_feature)
        if len(used) > limits.max_indicators:
            return f"too many indicators ({len(used)} > {limits.max_indicators})"
        if len(spec.entry.conditions) > limits.max_conditions:
            return (
                f"too many entry conditions "
                f"({len(spec.entry.conditions)} > {limits.max_conditions})"
            )
        if len(spec.exit.conditions) > limits.max_exit_conditions:
            return (
                f"too many exit conditions "
                f"({len(spec.exit.conditions)} > {limits.max_exit_conditions})"
            )
        if spec.complexity > limits.max_complexity:
            return f"too complex (score {spec.complexity} > {limits.max_complexity})"

        seen: set[tuple[str, str]] = set()
        for condition in spec.entry.conditions:
            key = (condition.feature, condition.op.value)
            if key in seen:
                return f"redundant condition {condition.feature} {condition.op.value}"
            seen.add(key)

        for condition in spec.entry.conditions:
            for exit_condition in spec.exit.conditions:
                if (
                    condition.feature == exit_condition.feature
                    and condition.ref_feature is None
                    and exit_condition.ref_feature is None
                    and abs(condition.value - exit_condition.value) < NARROW_PARAM_EPSILON
                ):
                    return (
                        f"narrow params on {condition.feature} "
                        f"({condition.value:g} vs {exit_condition.value:g})"
                    )
        return None


__all__ = [
    "ENTRY_BASES",
    "EXIT_RULES",
    "FILTER_COMBOS",
    "GenerationResult",
    "MOMENTUM_ENTRIES",
    "REGIME_GATE",
    "SearchLimits",
    "StrategyGenerator",
    "TREND_ENTRIES",
]
