"""Benchmarks — do the extra rules actually add value over naive alternatives?

All baselines are computed on the same window with the same net-of-cost
conventions as the strategy. Buy & hold is an equal-weight mark-to-market
basket; the index baseline is the same basket (the relevant market proxy for
the tested universe); the SMA200 filter is expressed as a rule strategy and
replayed through the production backtest engine; momentum is the engine's
default ema9/21 crossover; and the random baseline is a permutation control
over the strategy's own trade population (comparable exposure by construction).
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from qtrader.application.research.strategy.specs import (
    Condition,
    EntryRule,
    ExitRule,
    Operator,
    StrategySpec,
)
from qtrader.application.research.validation.records import (
    BenchmarkReport,
    BenchmarkSeriesResult,
    RandomBaselineResult,
)
from qtrader.domain.value_objects import PriceBar

_DEFAULT_MAX_POSITIONS = 10


def sma200_filter_spec(strategy_id: str) -> StrategySpec:
    """A buy-above / exit-below the 200-day SMA rule (the SMA200 benchmark)."""
    return StrategySpec(
        id=strategy_id,
        name="sma200-benchmark",
        entry=EntryRule(
            conditions=(
                Condition(feature="close", op=Operator.GT, ref_feature="sma_200"),
            )
        ),
        exit=ExitRule(
            conditions=(
                Condition(feature="close", op=Operator.LT, ref_feature="sma_200"),
            )
        ),
        features=("close", "sma_200"),
        complexity=2,
        description="long above 200-day SMA, flat below",
    )


def buy_and_hold_curve(
    bars_by_symbol: dict[str, list[PriceBar]],
    capital: Decimal,
    commission_bps: float,
    slippage_bps: float,
    max_positions: int = _DEFAULT_MAX_POSITIONS,
) -> list[tuple[datetime, Decimal]]:
    """Equal-weight buy & hold marked to market, net of entry/exit frictions.

    Each symbol receives an equal slice of ``capital`` bought at its first bar's
    open (slippage + commission charged), held to the final bar, and priced at
    close every day. Positions are capped at ``max_positions``.
    """
    symbols = [s for s, bars in bars_by_symbol.items() if bars]
    if not symbols:
        return []
    comm = Decimal(str(commission_bps)) / Decimal(10000)
    slip = Decimal(str(slippage_bps)) / Decimal(10000)
    budget = Decimal(capital) / Decimal(len(symbols))

    positions: dict[str, int] = {}
    cash = Decimal(capital)
    first_bar: dict[str, PriceBar] = {}
    for symbol in symbols[:max_positions]:
        bar = bars_by_symbol[symbol][0]
        first_bar[symbol] = bar
        price = bar.open * (Decimal(1) + slip)
        quantity = int(budget / price)
        if quantity < 1:
            continue
        positions[symbol] = quantity
        cash -= quantity * price * (Decimal(1) + comm)

    by_ts: dict[datetime, dict[str, PriceBar]] = defaultdict(dict)
    for symbol in symbols:
        for bar in bars_by_symbol[symbol]:
            by_ts[bar.ts][symbol] = bar

    curve: list[tuple[datetime, Decimal]] = []
    for ts in sorted(by_ts):
        equity = cash
        for symbol, bar in by_ts[ts].items():
            if symbol in positions:
                equity += positions[symbol] * bar.close
        curve.append((ts, equity))
    return curve


def random_permutation_result(
    trade_pnl_pcts: list[float],
    seeds: int,
) -> RandomBaselineResult:
    """Permute the strategy's own trade returns and compound them.

    The distribution of end-equity returns under random trade ordering answers:
    *given the same trades and exposure, is the observed sequence of results
    special?* A strategy whose return is inside the shuffled bulk has no
    ordering edge.
    """
    trades = len(trade_pnl_pcts)
    if trades == 0 or seeds <= 0:
        return RandomBaselineResult(seeds=seeds, trades=0, mean_total_return=0.0,
                                    p90_total_return=0.0, worst_total_return=0.0)
    outcomes: list[float] = []
    for seed in range(seeds):
        order = list(trade_pnl_pcts)
        rng = random.Random(seed)
        rng.shuffle(order)
        equity = 1.0
        for pnl in order:
            equity *= 1.0 + pnl
        outcomes.append(equity - 1.0)
    outcomes.sort()
    mean = sum(outcomes) / len(outcomes)
    p90 = outcomes[min(int(round(0.90 * len(outcomes))), len(outcomes) - 1)]
    return RandomBaselineResult(
        seeds=seeds,
        trades=trades,
        mean_total_return=round(mean, 6),
        p90_total_return=round(p90, 6),
        worst_total_return=round(outcomes[0], 6),
    )


@dataclass(frozen=True, slots=True)
class BenchmarkInputs:
    """Raw windowed outcomes handed to :func:`build_benchmark_report`."""

    strategy: BenchmarkSeriesResult
    buy_and_hold: BenchmarkSeriesResult
    index: BenchmarkSeriesResult
    sma200: BenchmarkSeriesResult
    momentum: BenchmarkSeriesResult
    random: RandomBaselineResult


def build_benchmark_report(inputs: BenchmarkInputs) -> BenchmarkReport:
    """Verdict: does the strategy beat the passive/naive baselines?"""
    strategy_return = inputs.strategy.total_return
    beats_buy_and_hold = _beats(strategy_return, inputs.buy_and_hold.total_return)
    beats_index = _beats(strategy_return, inputs.index.total_return)
    beats_sma200 = _beats(strategy_return, inputs.sma200.total_return)
    beats_random = _beats(strategy_return, inputs.random.mean_total_return)
    return BenchmarkReport(
        strategy=inputs.strategy,
        buy_and_hold=inputs.buy_and_hold,
        index=inputs.index,
        sma200=inputs.sma200,
        momentum=inputs.momentum,
        random=inputs.random,
        beats_buy_and_hold=beats_buy_and_hold,
        beats_index=beats_index,
        beats_sma200=beats_sma200,
        beats_random_mean=beats_random,
        value_added=beats_buy_and_hold and beats_sma200 and beats_random,
    )


def _beats(value: float | None, baseline: float | None) -> bool:
    if value is None or baseline is None:
        return False
    return value > baseline


__all__ = [
    "BenchmarkInputs",
    "build_benchmark_report",
    "buy_and_hold_curve",
    "random_permutation_result",
    "sma200_filter_spec",
]
