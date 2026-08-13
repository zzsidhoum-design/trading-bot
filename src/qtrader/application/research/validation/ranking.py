"""Multi-dimensional strategy ranking.

Ranking never uses raw historical return alone. Each surviving strategy is
scored by percentile ranks across expectancy, Sharpe, Sortino, drawdown,
profit factor, out-of-sample performance, walk-forward stability, trade
count, complexity (lower is better) and multiple-testing risk. The weighted
blend produces a single reproducible score; only VALIDATED strategies are
ranked.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from qtrader.application.research.validation.records import ValidationRecord

_MISSING = -1e18


@dataclass(frozen=True, slots=True)
class RankingWeights:
    """Weights for the rank-based composite score (must sum to 1.0)."""

    expectancy: float = 0.15
    sharpe: float = 0.15
    sortino: float = 0.10
    max_drawdown: float = 0.10
    profit_factor: float = 0.10
    oos_sharpe: float = 0.15
    wf_stability: float = 0.10
    trades: float = 0.05
    complexity: float = 0.05
    multiple_testing: float = 0.05

    def __post_init__(self) -> None:
        total = (
            self.expectancy
            + self.sharpe
            + self.sortino
            + self.max_drawdown
            + self.profit_factor
            + self.oos_sharpe
            + self.wf_stability
            + self.trades
            + self.complexity
            + self.multiple_testing
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"ranking weights must sum to 1.0 (got {total})")


@dataclass(frozen=True, slots=True)
class RankedStrategy:
    """One validated strategy's composite score and rank."""

    strategy_id: str
    score: float
    rank: int

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, RankedStrategy):
            return NotImplemented
        return (self.rank, self.strategy_id) < (other.rank, other.strategy_id)


def _metric_specs() -> tuple[tuple[str, bool, str], ...]:
    """(feature key, higher-is-better, weight attribute) for the blend."""
    return (
        ("expectancy", True, "expectancy"),
        ("sharpe", True, "sharpe"),
        ("sortino", True, "sortino"),
        ("max_drawdown", False, "max_drawdown"),
        ("profit_factor", True, "profit_factor"),
        ("oos_sharpe", True, "oos_sharpe"),
        ("wf_stability", True, "wf_stability"),
        ("trades", True, "trades"),
        ("complexity", False, "complexity"),
        ("multiple_testing", True, "multiple_testing"),
    )


class StrategyRanker:
    """Ranks VALIDATED strategies on a multi-dimensional weighted blend."""

    def __init__(self, weights: RankingWeights | None = None) -> None:
        self._weights = weights or RankingWeights()

    def rank(self, records: list[ValidationRecord]) -> list[RankedStrategy]:
        if not records:
            return []
        features = [self._features(record) for record in records]
        scores: list[tuple[str, float]] = []
        for idx, feature_row in enumerate(features):
            composite = 0.0
            for key, higher, weight_attr in _metric_specs():
                column = [row[key] for row in features]
                weight = getattr(self._weights, weight_attr)
                composite += weight * self._percentile(feature_row[key], column, higher=higher)
            scores.append((records[idx].strategy_id, round(composite, 6)))
        scores.sort(key=lambda item: item[1], reverse=True)
        return [
            RankedStrategy(strategy_id=strategy_id, score=score, rank=rank)
            for rank, (strategy_id, score) in enumerate(scores, start=1)
        ]

    def _features(self, record: ValidationRecord) -> dict[str, float]:
        oos = record.oos_result
        summary = oos.summary if oos is not None else None
        wf = record.wf_result
        mtest = record.multiple_testing
        stability = (
            (wf.stability_mean_sharpe or 0.0) - abs(wf.stability_std_sharpe or 0.0)
            if wf is not None
            else _MISSING
        )
        return {
            "expectancy": _opt(summary.expectancy) if summary is not None else _MISSING,
            "sharpe": _opt(summary.sharpe) if summary is not None else _MISSING,
            "sortino": _opt(summary.sortino) if summary is not None else _MISSING,
            "max_drawdown": _opt(summary.max_drawdown) if summary is not None else _MISSING,
            "profit_factor": _opt(summary.profit_factor) if summary is not None else _MISSING,
            "oos_sharpe": _opt(summary.sharpe) if summary is not None else _MISSING,
            "wf_stability": stability,
            "trades": float(summary.trades_count or 0) if summary is not None else _MISSING,
            "complexity": float(record.spec.complexity),
            "multiple_testing": _risk_score(mtest.risk) if mtest is not None else 0.0,
        }

    @staticmethod
    def _percentile(value: float, column: list[float], *, higher: bool) -> float:
        """Fraction of the column this value beats (0.0 when the value is absent)."""
        if value <= _MISSING / 2:
            return 0.0
        if not column:
            return 0.0
        if higher:
            return sum(1 for other in column if other < value) / len(column)
        return sum(1 for other in column if other > value) / len(column)


def _opt(value: Decimal | None) -> float:
    return _MISSING if value is None else float(value)


def _risk_score(risk: str) -> float:
    return {"low": 1.0, "medium": 0.5, "high": 0.0}.get(risk, 0.0)


__all__ = ["RankedStrategy", "RankingWeights", "StrategyRanker"]
