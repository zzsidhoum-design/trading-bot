"""Multi-Timeframe Research Engine (Phase 3).

Determines which timeframes and timeframe combinations are most useful for the
Strategy Research Engine -- explicitly *not* a trading strategy. Everything here
is pure, causal and unit-testable; the only I/O lives in
``MultitimeframeResearchEngine`` which loads bars through ``PriceRepository``.

What it produces (the 10 required outputs of the phase):

1. Performance of each timeframe (``analyze_timeframe`` / ``TimeframeStudy``).
2. Performance of each tested combination (``CombinationResult``).
3-5. Best context/setup/entry timeframes (``best_roles``).
6. Performance by market regime (regime attribution in ``StudyMetrics.regime``).
7. Transaction-cost sensitivity (``cost_sweep`` / ``CostPoint``).
8. Out-of-sample performance (walk-forward fold evaluation).
9. Walk-forward performance (``walk_forward`` / ``WalkForwardSummary``).
10. Final recommended combinations (``rank_recommendations``).

Signal model
------------
A parameterised trend/reversion signal (EMA cross or RSI reversion) is used
ONLY as a research harness to measure signals, holding periods and robustness
across timeframes. It is not a strategy that will ever be traded; the phase
explicitly defers strategy construction.

Roles
-----
Higher timeframe -> context (market filter), middle -> setup, lower -> entry.
``TimeframeCombo`` enforces strictly decreasing bar length. Slower timeframes
are aligned to the entry timeline by last-observation-carry-forward and act as
filters over the entry signal.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pandas as pd

from qtrader.application.services.market_regime import MarketRegimeEngine
from qtrader.domain.ports import PriceRepository, StockRepository, UniverseRepository
from qtrader.domain.value_objects import (
    Interval,
    PriceBar,
    derived_source,
    interval_minutes,
)

# --------------------------------------------------------------------------- #
# Interval metadata
# --------------------------------------------------------------------------- #

# Intraday bars per US session (09:30-16:00 ET = 390 minutes).
BARS_PER_DAY: dict[Interval, float] = {
    Interval.M1: 390.0,
    Interval.M5: 78.0,
    Interval.M15: 26.0,
    Interval.M30: 13.0,
    Interval.H1: 6.5,
    Interval.H4: 1.625,
    Interval.D1: 1.0,
}
BARS_PER_YEAR: dict[Interval, float] = {
    iv: bars * 252.0 for iv, bars in BARS_PER_DAY.items()
}

_DEFAULT_ANNUALIZATION = 252.0


def _bdate_count(start: date, end: date) -> int:
    """Number of business days in ``[start, end]`` (both inclusive)."""
    if start > end:
        return 0
    return len(pd.bdate_range(start, end))


def _weekend_days(d1: date, d2: date) -> int:
    count = 0
    day = d1 + timedelta(days=1)
    while day < d2:
        if day.weekday() >= 5:
            count += 1
        day += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Data quality per timeframe
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TimeframeQuality:
    """Coverage / hygiene of the persisted bars for one (symbol, interval)."""

    interval: Interval
    bars: int
    first_ts: datetime | None
    last_ts: datetime | None
    coverage_pct: float
    max_gap_bars: float
    aligned_pct: float
    ok: bool


def _on_grid(ts: datetime, interval: Interval) -> bool:
    minutes = interval_minutes(interval)
    if interval is Interval.D1:
        return True
    if minutes >= 240:
        return ts.second == 0 and ts.minute == 0 and ts.hour % (minutes // 60) == 0
    if minutes >= 60:
        return ts.second == 0 and ts.minute == 0
    return ts.second == 0 and ts.minute % minutes == 0


def timeframe_quality(
    bars: Sequence[PriceBar],
    interval: Interval,
    *,
    min_coverage_pct: float = 0.9,
    min_aligned_pct: float = 0.95,
) -> TimeframeQuality:
    """Structural quality stats for a persisted bar series (pure)."""
    ordered = sorted(bars, key=lambda b: b.ts)
    if not ordered:
        return TimeframeQuality(
            interval=interval,
            bars=0,
            first_ts=None,
            last_ts=None,
            coverage_pct=0.0,
            max_gap_bars=0.0,
            aligned_pct=0.0,
            ok=False,
        )
    first = ordered[0].ts
    last = ordered[-1].ts
    expected = _bdate_count(first.date(), last.date()) * BARS_PER_DAY[interval]
    coverage = min(1.0, len(ordered) / expected) if expected > 0 else 0.0

    minutes = interval_minutes(interval)
    max_gap = 0.0
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        delta_min = (cur.ts - prev.ts).total_seconds() / 60.0
        if interval is Interval.D1:
            weekend = _weekend_days(prev.ts.date(), cur.ts.date())
            gap = max(0.0, delta_min / minutes - 1.0 - weekend)
        elif prev.ts.date() == cur.ts.date():
            gap = max(0.0, delta_min / minutes - 1.0)
        else:
            continue
        max_gap = max(max_gap, gap)

    aligned = (
        sum(1 for b in ordered if _on_grid(b.ts, interval)) / len(ordered)
        if interval is not Interval.D1
        else 1.0
    )

    return TimeframeQuality(
        interval=interval,
        bars=len(ordered),
        first_ts=first,
        last_ts=last,
        coverage_pct=round(coverage, 4),
        max_gap_bars=round(max_gap, 2),
        aligned_pct=round(aligned, 4),
        ok=coverage >= min_coverage_pct
        and aligned >= min_aligned_pct
        and len(ordered) >= 2,
    )


# --------------------------------------------------------------------------- #
# Resampling (derive coarser intervals from finer persisted ones)
# --------------------------------------------------------------------------- #


def _bucket_ts(ts: datetime, minutes: int) -> datetime:
    if minutes < 60:
        return ts.replace(
            minute=(ts.minute // minutes) * minutes, second=0, microsecond=0
        )
    step_hours = minutes // 60
    return ts.replace(
        hour=(ts.hour // step_hours) * step_hours, minute=0, second=0, microsecond=0
    )


def resample_bars(
    bars: Sequence[PriceBar],
    *,
    target: Interval,
    source: Interval,
) -> list[PriceBar]:
    """Aggregate ``source`` bars into a coarser ``target`` interval (pure).

    Used to derive intervals the provider cannot serve natively (H4 from H1).
    Buckets align to the UTC clock grid of the target interval; OHLCV is
    aggregated open-high-low-close-volume. Returns [] when no bucket can be
    built.
    """
    if interval_minutes(target) <= interval_minutes(source):
        raise ValueError(
            f"resample target {target.value} must be coarser than source "
            f"{source.value}"
        )
    if target is Interval.D1:
        raise ValueError("resample to D1 is not supported; fetch D1 natively")
    minutes = interval_minutes(target)
    buckets: dict[datetime, dict[str, Any]] = {}
    order: list[datetime] = []
    symbol = bars[0].symbol if bars else ""
    for bar in sorted(bars, key=lambda b: b.ts):
        key = _bucket_ts(bar.ts, minutes)
        if key not in buckets:
            buckets[key] = {
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            }
            order.append(key)
        else:
            bucket = buckets[key]
            bucket["high"] = max(bucket["high"], float(bar.high))
            bucket["low"] = min(bucket["low"], float(bar.low))
            bucket["close"] = float(bar.close)
            bucket["volume"] += float(bar.volume)
    from decimal import Decimal

    return [
        PriceBar(
            symbol=symbol,
            interval=target,
            ts=key,
            open=Decimal(str(bucket["open"])),
            high=Decimal(str(bucket["high"])),
            low=Decimal(str(bucket["low"])),
            close=Decimal(str(bucket["close"])),
            volume=Decimal(str(bucket["volume"])),
        )
        for key, bucket in ((key, buckets[key]) for key in order)
    ]


# --------------------------------------------------------------------------- #
# Research signal harness (NOT a tradeable strategy)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SignalParams:
    """Parameterised trend/reversion signal used to compare timeframes."""

    mode: str = "trend"  # "trend" | "reversion"
    fast: int = 9
    slow: int = 21
    band: float = 0.0  # trend dead-band as fraction of close
    rsi_entry: float = 30.0
    rsi_exit: float = 55.0


def _ema(values: Sequence[float], period: int) -> list[float]:
    n = len(values)
    out: list[float] = [float("nan")] * n
    if n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1)
    for i in range(period, n):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def _rsi(values: Sequence[float], period: int) -> list[float]:
    n = len(values)
    out: list[float] = [float("nan")] * n
    if n < period + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def signal_series(bars: Sequence[PriceBar], params: SignalParams) -> list[int]:
    """Per-bar target position (+1 long / 0 flat / -1 short) aligned to bars.

    Stateful: once the position flips it is held until the opposite threshold
    is crossed (a dead-band / exit band), which is what makes "average holding
    period" measurable. The first bar's signal is never tradeable (simulator
    applies signals at the next bar's open).
    """
    closes = [float(b.close) for b in bars]
    n = len(closes)
    out: list[int] = [0] * n
    if n < 2:
        return out
    prev = 0
    if params.mode == "trend":
        fast = _ema(closes, params.fast)
        slow = _ema(closes, params.slow)
        for i in range(n):
            if math.isnan(fast[i]) or math.isnan(slow[i]) or closes[i] <= 0:
                out[i] = 0
                prev = 0
                continue
            rel = (fast[i] - slow[i]) / closes[i]
            if rel > params.band:
                cur = 1
            elif rel < -params.band:
                cur = -1
            else:
                cur = prev
            out[i] = cur
            prev = cur
    else:  # reversion: long-only mean reversion on RSI
        rsi = _rsi(closes, 14)
        for i in range(n):
            if math.isnan(rsi[i]):
                out[i] = 0
                prev = 0
                continue
            if rsi[i] < params.rsi_entry:
                cur = 1
            elif rsi[i] > params.rsi_exit:
                cur = 0
            else:
                cur = prev
            out[i] = cur
            prev = cur
    return out


# --------------------------------------------------------------------------- #
# Timeframe roles & combination
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TimeframeCombo:
    """One context->setup->entry combination, strictly decreasing in length."""

    context: Interval
    setup: Interval
    entry: Interval

    @property
    def key(self) -> str:
        return f"{self.context.value}->{self.setup.value}->{self.entry.value}"

    def __post_init__(self) -> None:
        if not (
            interval_minutes(self.context)
            > interval_minutes(self.setup)
            > interval_minutes(self.entry)
        ):
            raise ValueError(
                "combo intervals must be strictly decreasing: "
                f"{self.key}"
            )


def enumerate_combos(intervals: Iterable[Interval]) -> list[TimeframeCombo]:
    """All valid (context, setup, entry) triples from ``intervals``."""
    ivs = sorted(
        {iv for iv in intervals}, key=interval_minutes, reverse=True
    )
    combos: list[TimeframeCombo] = []
    for i, context in enumerate(ivs):
        for j in range(i + 1, len(ivs)):
            for entry in ivs[j + 1 :]:
                combos.append(
                    TimeframeCombo(context=context, setup=ivs[j], entry=entry)
                )
    return combos


def align_latest(
    slow: Sequence[tuple[datetime, int]], entry_bars: Sequence[PriceBar]
) -> list[int]:
    """Carry the latest slower-timeframe signal forward onto the entry bars.

    ``slow`` must be sorted by timestamp; only signals with ts <= the entry
    bar's ts are used (no look-ahead). Missing history -> 0 (flat).
    """
    out: list[int] = [0] * len(entry_bars)
    j = -1
    for i, bar in enumerate(entry_bars):
        while j + 1 < len(slow) and slow[j + 1][0] <= bar.ts:
            j += 1
        if j >= 0:
            out[i] = slow[j][1]
    return out


def combine_signals(
    entry: Sequence[int],
    setup: Sequence[int],
    context: Sequence[int],
    *,
    mode: str = "all",
) -> list[int]:
    """Combine role signals into one target position per entry bar.

    ``all``: entry signal must agree with (or be unopposed by) the slower
    roles -- slower timeframes act as filters. ``majority``: >= 2 of 3 must
    agree. ``entry``: only the entry timeframe decides.
    """
    if mode == "entry":
        return list(entry)
    out: list[int] = []
    for e, s, c in zip(entry, setup, context, strict=True):
        if mode == "majority":
            total = e + s + c
            out.append(1 if total >= 1 else -1 if total <= -1 else 0)
            continue
        if e == 1 and s >= 0 and c >= 0:
            out.append(1)
        elif e == -1 and s <= 0 and c <= 0:
            out.append(-1)
        else:
            out.append(0)
    return out


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SimParams:
    """Cost model for the research simulator (bps = 1e-4)."""

    commission_bps: float = 10.0
    slippage_bps: float = 50.0
    max_hold_bars: int = 0  # 0 disables the time stop
    initial_equity: float = 100_000.0


@dataclass(frozen=True, slots=True)
class ResearchTrade:
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    pnl_pct: float  # net of costs
    costs_pct: float  # gross - net impact in pct points
    holding_bars: int


@dataclass(frozen=True, slots=True)
class SimResult:
    trades: list[ResearchTrade]
    equity: list[tuple[datetime, float]]  # (bar close ts, equity)
    total_return: float
    n_flips: int


def simulate(
    bars: Sequence[PriceBar],
    target: Sequence[int],
    *,
    params: SimParams,
) -> SimResult:
    """Long/flat simulation with next-bar-open fills and cost model (pure).

    ``target[i]`` is the position desired *after* bar ``i``; it is entered at
    bar ``i+1``'s open. Full investment when long (equal-weight sizing), flat
    in cash otherwise. Open positions are liquidated at the final bar's close.
    """
    ordered = sorted(bars, key=lambda b: b.ts)
    n = len(ordered)
    if n < 2:
        return SimResult(trades=[], equity=[], total_return=0.0, n_flips=0)

    pos = 0
    cash = params.initial_equity
    qty = 0.0
    entry_price = 0.0
    invested = 0.0
    entry_ts: datetime | None = None
    held = 0
    trades: list[ResearchTrade] = []
    equity: list[tuple[datetime, float]] = []
    flips = 0

    def _enter(bar: PriceBar) -> None:
        nonlocal cash, qty, entry_price, invested, entry_ts, held, pos, flips
        fill = float(bar.open) * (1.0 + params.slippage_bps * 1e-4)
        fee = cash * params.commission_bps * 1e-4
        invested = cash
        qty = (cash - fee) / fill
        entry_price = fill
        entry_ts = bar.ts
        held = 0
        cash = invested
        pos = 1
        flips += 1

    def _exit(bar: PriceBar) -> None:
        nonlocal cash, qty, pos, flips, entry_price, entry_ts, held
        fill = float(bar.open) * (1.0 - params.slippage_bps * 1e-4)
        proceeds = qty * fill
        fee = proceeds * params.commission_bps * 1e-4
        cash = proceeds - fee
        assert entry_ts is not None and invested > 0
        gross = (fill - entry_price) / entry_price * 100.0
        net = (cash - invested) / invested * 100.0
        trades.append(
            ResearchTrade(
                entry_ts=entry_ts,
                exit_ts=bar.ts,
                entry_price=entry_price,
                exit_price=fill,
                pnl_pct=round(net, 6),
                costs_pct=round(gross - net, 6),
                holding_bars=held,
            )
        )
        qty = 0.0
        entry_price = 0.0
        entry_ts = None
        pos = 0
        flips += 1

    for i, bar in enumerate(ordered):
        desired = target[i - 1] if i >= 1 else 0
        if pos == 1 and params.max_hold_bars > 0 and held >= params.max_hold_bars:
            desired = 0
        if pos == 1 and desired != 1:
            _exit(bar)
        if pos == 0 and desired == 1:
            _enter(bar)
        if pos == 1:
            held += 1
        if pos == 1:
            equity.append((bar.ts, qty * float(bar.close)))
        else:
            equity.append((bar.ts, cash))

    if pos == 1:
        last = ordered[-1]
        fill = float(last.close) * (1.0 - params.slippage_bps * 1e-4)
        proceeds = qty * fill
        fee = proceeds * params.commission_bps * 1e-4
        cash = proceeds - fee
        gross = (fill - entry_price) / entry_price * 100.0
        net = (cash - invested) / invested * 100.0
        trades.append(
            ResearchTrade(
                entry_ts=entry_ts,  # type: ignore[arg-type]
                exit_ts=last.ts,
                entry_price=entry_price,
                exit_price=fill,
                pnl_pct=round(net, 6),
                costs_pct=round(gross - net, 6),
                holding_bars=held,
            )
        )
        equity.append((last.ts, cash))

    total_return = (cash / params.initial_equity - 1.0) * 100.0
    return SimResult(
        trades=trades,
        equity=equity,
        total_return=round(total_return, 6),
        n_flips=flips,
    )


# --------------------------------------------------------------------------- #
# Metrics, regimes, cost sensitivity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RegimeSlice:
    regime: str
    n_trades: int
    total_return_pct: float
    win_rate: float


@dataclass(frozen=True, slots=True)
class StudyMetrics:
    total_return: float
    expected_value: float  # mean net pnl % per trade
    sharpe: float  # daily-resampled equity, annualized 252
    sortino: float
    max_drawdown: float  # positive fraction
    profit_factor: float
    win_rate: float
    n_trades: int
    winning_trades: int
    avg_holding_bars: float
    stable_trades: int  # trades held >= 3 bars
    costs_pct: float  # total cost impact across trades (pct points)
    gross_profit_pct: float
    gross_loss_pct: float
    signal_stability: float  # fraction of trades held >= 3 bars
    regime: dict[str, RegimeSlice] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DayRegime:
    date: date
    market: str  # bull | bear | sideways
    volatility: str  # low | high


def regime_labels_for(bars_d1: Sequence[PriceBar]) -> list[DayRegime]:
    """Causal per-day regime tags from the symbol's own D1 closes."""
    closes = [(b.ts, float(b.close)) for b in bars_d1]
    labels = MarketRegimeEngine().classify(closes)
    out: list[DayRegime] = []
    for lbl in labels:
        if lbl.market is None or lbl.volatility is None:
            continue
        out.append(
            DayRegime(
                date=lbl.ts.date(),
                market=lbl.market.value,
                volatility="high" if lbl.volatility.value == "extreme" else lbl.volatility.value,
            )
        )
    return out


def _assign_regimes(
    trades: Sequence[ResearchTrade], regimes: Sequence[DayRegime]
) -> dict[str, RegimeSlice]:
    dates = [r.date for r in regimes]
    buckets: dict[str, list[ResearchTrade]] = {}
    for trade in trades:
        idx = bisect_right(dates, trade.entry_ts.date()) - 1
        if idx < 0:
            continue
        day = regimes[idx]
        for key in (f"market:{day.market}", f"vol:{day.volatility}"):
            buckets.setdefault(key, []).append(trade)
    out: dict[str, RegimeSlice] = {}
    for key, subset in buckets.items():
        wins = sum(1 for t in subset if t.pnl_pct > 0)
        out[key] = RegimeSlice(
            regime=key,
            n_trades=len(subset),
            total_return_pct=round(sum(t.pnl_pct for t in subset), 6),
            win_rate=round(wins / len(subset), 4),
        )
    return out


def _daily_equity(equity: Sequence[tuple[datetime, float]]) -> list[tuple[date, float]]:
    by_date: dict[date, float] = {}
    for ts, value in equity:
        by_date[ts.date()] = value
    return sorted(by_date.items())


def _sharpe(returns: Sequence[float], annualization: float = 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    std = statistics.pstdev(returns)
    if std == 0:
        return 0.0
    return mean / std * math.sqrt(annualization)


def _sortino(returns: Sequence[float], annualization: float = 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    dstd = statistics.pstdev(downside)
    if dstd == 0:
        return 0.0
    return mean / dstd * math.sqrt(annualization)


def _max_drawdown(equity: Sequence[tuple[date, float]]) -> float:
    peak = -1.0
    worst = 0.0
    for _, value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    return abs(worst)


def compute_study_metrics(
    result: SimResult, *, regime: Sequence[DayRegime] | None = None
) -> StudyMetrics:
    """Statistical summary of one simulation (pure)."""
    trades = result.trades
    n = len(trades)
    if n == 0:
        return StudyMetrics(
            total_return=result.total_return,
            expected_value=0.0,
            sharpe=0.0,
            sortino=0.0,
            max_drawdown=_max_drawdown(_daily_equity(result.equity)),
            profit_factor=0.0,
            win_rate=0.0,
            n_trades=0,
            winning_trades=0,
            avg_holding_bars=0.0,
            stable_trades=0,
            costs_pct=0.0,
            gross_profit_pct=0.0,
            gross_loss_pct=0.0,
            signal_stability=0.0,
            regime=_assign_regimes(trades, regime) if regime else {},
        )

    pnls = [t.pnl_pct for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    daily = _daily_equity(result.equity)
    daily_returns = [
        (v1 - v0) / v0 for (_, v0), (_, v1) in zip(daily, daily[1:], strict=False) if v0 > 0
    ]

    stable = sum(1 for t in trades if t.holding_bars >= 3)
    return StudyMetrics(
        total_return=result.total_return,
        expected_value=round(statistics.fmean(pnls), 6),
        sharpe=round(_sharpe(daily_returns), 4),
        sortino=round(_sortino(daily_returns), 4),
        max_drawdown=round(_max_drawdown(daily), 4),
        profit_factor=round(gross_profit / gross_loss, 4) if gross_loss > 0 else 0.0,
        win_rate=round(len(wins) / n, 4),
        n_trades=n,
        winning_trades=len(wins),
        avg_holding_bars=round(statistics.fmean(t.holding_bars for t in trades), 2),
        stable_trades=stable,
        costs_pct=round(sum(t.costs_pct for t in trades), 4),
        gross_profit_pct=round(gross_profit, 4),
        gross_loss_pct=round(gross_loss, 4),
        signal_stability=round(stable / n, 4),
        regime=_assign_regimes(trades, regime) if regime else {},
    )


@dataclass(frozen=True, slots=True)
class CostPoint:
    commission_bps: float
    slippage_bps: float
    total_return: float
    sharpe: float
    costs_pct: float


_COST_COMMISSIONS = (0.0, 5.0, 10.0, 25.0, 50.0, 100.0)
_COST_SLIPPAGES = (0.0, 10.0, 50.0, 100.0)


def cost_sweep(
    bars: Sequence[PriceBar],
    target: Sequence[int],
    *,
    params: SimParams,
) -> list[CostPoint]:
    """Total-return / Sharpe at rising commission and slippage levels (pure)."""
    points: list[CostPoint] = []
    for comm in _COST_COMMISSIONS:
        res = simulate(bars, target, params=replace(params, commission_bps=comm))
        metrics = compute_study_metrics(res)
        points.append(
            CostPoint(
                commission_bps=comm,
                slippage_bps=params.slippage_bps,
                total_return=res.total_return,
                sharpe=metrics.sharpe,
                costs_pct=metrics.costs_pct,
            )
        )
    for slip in _COST_SLIPPAGES:
        res = simulate(bars, target, params=replace(params, slippage_bps=slip))
        metrics = compute_study_metrics(res)
        points.append(
            CostPoint(
                commission_bps=params.commission_bps,
                slippage_bps=slip,
                total_return=res.total_return,
                sharpe=metrics.sharpe,
                costs_pct=metrics.costs_pct,
            )
        )
    return points


def aggregate_metrics(per_symbol: dict[str, StudyMetrics]) -> StudyMetrics:
    """Pool per-symbol results into one comparable metric block (equal weight).

    Return-style fields are averaged; trade-level fields are pooled exactly
    (win rate, profit factor, holding, stability all use sums/counts).
    """
    items = list(per_symbol.values())
    if not items:
        return StudyMetrics(
            total_return=0.0, expected_value=0.0, sharpe=0.0, sortino=0.0,
            max_drawdown=0.0, profit_factor=0.0, win_rate=0.0, n_trades=0,
            winning_trades=0, avg_holding_bars=0.0, stable_trades=0,
            costs_pct=0.0, gross_profit_pct=0.0, gross_loss_pct=0.0,
            signal_stability=0.0,
        )
    total_trades = sum(m.n_trades for m in items)
    total_wins = sum(m.winning_trades for m in items)
    total_stable = sum(m.stable_trades for m in items)
    gross_profit = sum(m.gross_profit_pct for m in items)
    gross_loss = sum(m.gross_loss_pct for m in items)
    avg_holding = (
        sum(m.avg_holding_bars * m.n_trades for m in items) / total_trades
        if total_trades
        else 0.0
    )
    ev = (
        sum(m.expected_value * m.n_trades for m in items) / total_trades
        if total_trades
        else 0.0
    )
    regime: dict[str, RegimeSlice] = {}
    for m in items:
        for key, slice_ in m.regime.items():
            prev = regime.get(key)
            if prev is None:
                regime[key] = slice_
            else:
                wins = prev.n_trades * prev.win_rate + slice_.n_trades * slice_.win_rate
                regime[key] = RegimeSlice(
                    regime=key,
                    n_trades=prev.n_trades + slice_.n_trades,
                    total_return_pct=round(
                        prev.total_return_pct + slice_.total_return_pct, 6
                    ),
                    win_rate=round(
                        wins / (prev.n_trades + slice_.n_trades), 4
                    )
                    if (prev.n_trades + slice_.n_trades) > 0
                    else 0.0,
                )
    return StudyMetrics(
        total_return=round(statistics.fmean(m.total_return for m in items), 4),
        expected_value=round(ev, 6),
        sharpe=round(statistics.fmean(m.sharpe for m in items), 4),
        sortino=round(statistics.fmean(m.sortino for m in items), 4),
        max_drawdown=round(statistics.fmean(m.max_drawdown for m in items), 4),
        profit_factor=round(gross_profit / gross_loss, 4) if gross_loss > 0 else 0.0,
        win_rate=round(total_wins / total_trades, 4) if total_trades else 0.0,
        n_trades=total_trades,
        winning_trades=total_wins,
        avg_holding_bars=round(avg_holding, 2),
        stable_trades=total_stable,
        costs_pct=round(statistics.fmean(m.costs_pct for m in items), 4),
        gross_profit_pct=round(gross_profit, 4),
        gross_loss_pct=round(gross_loss, 4),
        signal_stability=round(total_stable / total_trades, 4)
        if total_trades
        else 0.0,
        regime=regime,
    )


# --------------------------------------------------------------------------- #
# Single-timeframe analysis
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TimeframeStudy:
    interval: Interval
    quality: TimeframeQuality
    noise_mean_abs_ret: float  # mean |return| per bar
    volatility_annualized: float
    signals_per_day: float  # trade entries per business day
    avg_holding_bars: float
    n_trades: int
    total_return: float
    sharpe: float
    signal_stability: float
    cost_sensitivity: list[CostPoint]


def analyze_timeframe(
    bars: Sequence[PriceBar],
    interval: Interval,
    *,
    params: SignalParams,
    sim_params: SimParams,
    min_coverage_pct: float = 0.9,
) -> TimeframeStudy:
    """Performance + noise + stability of a single timeframe (pure)."""
    quality = timeframe_quality(bars, interval, min_coverage_pct=min_coverage_pct)
    ordered = sorted(bars, key=lambda b: b.ts)
    closes = [float(b.close) for b in ordered]
    returns = [
        (cur - prev) / prev for prev, cur in zip(closes, closes[1:], strict=False) if prev > 0
    ]
    mean_abs = statistics.fmean(abs(r) for r in returns) if returns else 0.0
    vol = (
        statistics.pstdev(returns) * math.sqrt(BARS_PER_YEAR[interval])
        if len(returns) >= 2
        else 0.0
    )
    target = signal_series(ordered, params)
    result = simulate(ordered, target, params=sim_params)
    metrics = compute_study_metrics(result)
    span_days = (
        _bdate_count(ordered[0].ts.date(), ordered[-1].ts.date())
        if ordered
        else 0
    )
    return TimeframeStudy(
        interval=interval,
        quality=quality,
        noise_mean_abs_ret=round(mean_abs, 6),
        volatility_annualized=round(vol, 4),
        signals_per_day=round(result.n_flips / span_days, 4) if span_days else 0.0,
        avg_holding_bars=metrics.avg_holding_bars,
        n_trades=metrics.n_trades,
        total_return=metrics.total_return,
        sharpe=metrics.sharpe,
        signal_stability=metrics.signal_stability,
        cost_sensitivity=cost_sweep(ordered, target, params=sim_params),
    )


# --------------------------------------------------------------------------- #
# Combination study, walk-forward robustness, parameter sensitivity
# --------------------------------------------------------------------------- #


def _combo_metrics(
    entry_bars: Sequence[PriceBar],
    bars_by_interval: dict[Interval, list[PriceBar]],
    combo: TimeframeCombo,
    *,
    params: SignalParams,
    sim_params: SimParams,
    mode: str,
    regime: Sequence[DayRegime] | None,
) -> StudyMetrics:
    target_entry = signal_series(entry_bars, params)
    setup_sig = align_latest(
        list(
            zip(
                (b.ts for b in bars_by_interval[combo.setup]),
                signal_series(bars_by_interval[combo.setup], params),
                strict=True,
            )
        ),
        entry_bars,
    )
    context_sig = align_latest(
        list(
            zip(
                (b.ts for b in bars_by_interval[combo.context]),
                signal_series(bars_by_interval[combo.context], params),
                strict=True,
            )
        ),
        entry_bars,
    )
    combined = combine_signals(target_entry, setup_sig, context_sig, mode=mode)
    result = simulate(entry_bars, combined, params=sim_params)
    return compute_study_metrics(result, regime=regime)


@dataclass(frozen=True, slots=True)
class WfFold:
    fold: int
    start_ts: datetime | None
    end_ts: datetime | None
    train_sharpe: float
    chosen_params: SignalParams
    oos_total_return: float
    oos_sharpe: float
    oos_trades: int


@dataclass(frozen=True, slots=True)
class WalkForwardSummary:
    n_folds: int
    oos_sharpe_mean: float
    oos_sharpe_positive_ratio: float
    oos_return_mean: float
    oos_return_positive_ratio: float
    oos_trades: int
    folds: list[WfFold] = field(default_factory=list)


def walk_forward(
    entry_bars: Sequence[PriceBar],
    bars_by_interval: dict[Interval, list[PriceBar]],
    combo: TimeframeCombo,
    param_grid: Sequence[SignalParams],
    *,
    sim_params: SimParams,
    n_folds: int = 4,
    min_train_bars: int = 100,
    mode: str = "all",
    regime: Sequence[DayRegime] | None = None,
) -> WalkForwardSummary:
    """Chronological walk-forward: choose params on train, evaluate on OOS.

    Only the OOS fold metrics are returned as the honest estimate; the train
    fold is where the parameter pick happens (so ranking a combo by its OOS
    Sharpe is selection-bias free).
    """
    ordered = sorted(entry_bars, key=lambda b: b.ts)
    n = len(ordered)
    if n < n_folds * min_train_bars:
        return WalkForwardSummary(n_folds=0, oos_sharpe_mean=0.0,
                                  oos_sharpe_positive_ratio=0.0,
                                  oos_return_mean=0.0,
                                  oos_return_positive_ratio=0.0, oos_trades=0)

    folds: list[WfFold] = []
    oos_returns: list[float] = []
    oos_sharpes: list[float] = []
    oos_trades = 0
    for f in range(n_folds):
        start_idx = f * n // n_folds
        end_idx = (f + 1) * n // n_folds if f < n_folds - 1 else n
        train = ordered[:start_idx]
        test = ordered[start_idx:end_idx]
        if len(train) < min_train_bars or len(test) < 2:
            continue
        best_params: SignalParams | None = None
        best_sharpe = -float("inf")
        for params in param_grid:
            m = _combo_metrics(
                train, bars_by_interval, combo, params=params,
                sim_params=sim_params, mode=mode, regime=None,
            )
            if m.sharpe > best_sharpe:
                best_sharpe = m.sharpe
                best_params = params
        assert best_params is not None
        oos = _combo_metrics(
            test, bars_by_interval, combo, params=best_params,
            sim_params=sim_params, mode=mode, regime=regime,
        )
        folds.append(
            WfFold(
                fold=f,
                start_ts=test[0].ts,
                end_ts=test[-1].ts,
                train_sharpe=round(best_sharpe, 4),
                chosen_params=best_params,
                oos_total_return=oos.total_return,
                oos_sharpe=oos.sharpe,
                oos_trades=oos.n_trades,
            )
        )
        oos_returns.append(oos.total_return)
        oos_sharpes.append(oos.sharpe)
        oos_trades += oos.n_trades

    return WalkForwardSummary(
        n_folds=len(folds),
        oos_sharpe_mean=round(statistics.fmean(oos_sharpes), 4)
        if oos_sharpes else 0.0,
        oos_sharpe_positive_ratio=round(
            sum(1 for s in oos_sharpes if s > 0) / len(oos_sharpes), 4
        ) if oos_sharpes else 0.0,
        oos_return_mean=round(statistics.fmean(oos_returns), 4)
        if oos_returns else 0.0,
        oos_return_positive_ratio=round(
            sum(1 for r in oos_returns if r > 0) / len(oos_returns), 4
        ) if oos_returns else 0.0,
        oos_trades=oos_trades,
        folds=folds,
    )


@dataclass(frozen=True, slots=True)
class ParamSensitivity:
    grid_size: int
    sharpe_mean: float
    sharpe_std: float
    sharpe_positive_ratio: float
    max_drawdown_mean: float
    best_params: SignalParams
    best_sharpe: float


def parameter_sensitivity(
    entry_bars: Sequence[PriceBar],
    bars_by_interval: dict[Interval, list[PriceBar]],
    combo: TimeframeCombo,
    param_grid: Sequence[SignalParams],
    *,
    sim_params: SimParams,
    mode: str = "all",
) -> ParamSensitivity:
    """Full-history performance spread across the signal parameter grid."""
    sharpes: list[float] = []
    drawdowns: list[float] = []
    best_params: SignalParams | None = None
    best_sharpe = -float("inf")
    for params in param_grid:
        m = _combo_metrics(
            entry_bars, bars_by_interval, combo, params=params,
            sim_params=sim_params, mode=mode, regime=None,
        )
        sharpes.append(m.sharpe)
        drawdowns.append(m.max_drawdown)
        if m.sharpe > best_sharpe:
            best_sharpe = m.sharpe
            best_params = params
    assert best_params is not None
    return ParamSensitivity(
        grid_size=len(param_grid),
        sharpe_mean=round(statistics.fmean(sharpes), 4) if sharpes else 0.0,
        sharpe_std=round(statistics.pstdev(sharpes), 4) if len(sharpes) > 1 else 0.0,
        sharpe_positive_ratio=round(
            sum(1 for s in sharpes if s > 0) / len(sharpes), 4
        ) if sharpes else 0.0,
        max_drawdown_mean=round(statistics.fmean(drawdowns), 4)
        if drawdowns else 0.0,
        best_params=best_params,
        best_sharpe=round(best_sharpe, 4),
    )


# --------------------------------------------------------------------------- #
# Recommendation engine
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CombinationResult:
    combo: TimeframeCombo
    metrics: StudyMetrics  # full history, pooled across symbols
    per_symbol: dict[str, StudyMetrics]
    cost_sensitivity: list[CostPoint]
    walk_forward: WalkForwardSummary
    param_sensitivity: ParamSensitivity


@dataclass(frozen=True, slots=True)
class Recommendation:
    combo: TimeframeCombo
    score: float
    robustness: str  # HIGH | MEDIUM | LOW
    expected_value: float
    sharpe: float
    max_drawdown: float
    profit_factor: float
    oos_sharpe: float
    oos_total_return: float
    param_stability: float
    regime_consistency: float


def _regime_consistency(regime: dict[str, RegimeSlice], min_trades: int = 3) -> float:
    slices = [s for s in regime.values() if s.n_trades >= min_trades]
    if not slices:
        return 0.0
    positive = sum(1 for s in slices if s.total_return_pct > 0)
    return positive / len(slices)


def rank_recommendations(
    combinations: Sequence[CombinationResult],
) -> list[Recommendation]:
    """Rank combos on OOS-first evidence, not raw historical return."""
    recs: list[Recommendation] = []
    for c in combinations:
        wf = c.walk_forward
        ps = c.param_sensitivity
        m = c.metrics
        oos_sharpe = wf.oos_sharpe_mean
        oos_return = wf.oos_return_mean
        oos_pos = 1.0 if oos_return > 0 else -1.0
        stability = ps.sharpe_positive_ratio
        dd_term = 1.0 - min(m.max_drawdown, 1.0)
        regime_cons = _regime_consistency(m.regime)
        score = (
            0.30 * math.tanh(oos_sharpe)
            + 0.20 * oos_pos
            + 0.20 * stability
            + 0.15 * dd_term
            + 0.15 * regime_cons
        )
        if oos_sharpe <= 0 and oos_return <= 0:
            robustness = "LOW"
        elif score >= 0.35 and stability >= 0.6 and oos_sharpe > 0:
            robustness = "HIGH"
        else:
            robustness = "MEDIUM"
        recs.append(
            Recommendation(
                combo=c.combo,
                score=round(score, 4),
                robustness=robustness,
                expected_value=m.expected_value,
                sharpe=m.sharpe,
                max_drawdown=m.max_drawdown,
                profit_factor=m.profit_factor,
                oos_sharpe=wf.oos_sharpe_mean,
                oos_total_return=wf.oos_return_mean,
                param_stability=ps.sharpe_positive_ratio,
                regime_consistency=round(regime_cons, 4),
            )
        )
    return sorted(recs, key=lambda r: r.score, reverse=True)


def best_roles(
    recommendations: Sequence[Recommendation], top_n: int = 10
) -> tuple[Interval, Interval, Interval]:
    """Score-weighted most-useful timeframe per role over the top combos."""
    roles = ("context", "setup", "entry")
    best: dict[str, Interval | None] = {}
    for role in roles:
        scores: dict[Interval, float] = {}
        for rec in recommendations[:top_n]:
            iv = getattr(rec.combo, role)
            scores[iv] = scores.get(iv, 0.0) + rec.score
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best[role] = ranked[0][0] if ranked else None
    return (
        best["context"] or Interval.D1,
        best["setup"] or Interval.H1,
        best["entry"] or Interval.M5,
    )


# --------------------------------------------------------------------------- #
# Report + orchestration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ResearchReport:
    as_of: date
    symbols: tuple[str, ...]
    start: date
    end: date
    timeframe_studies: list[TimeframeStudy]
    combinations: list[CombinationResult]
    recommendations: list[Recommendation]
    best_context: Interval
    best_setup: Interval
    best_entry: Interval
    limitations: tuple[str, ...] = ()


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, (Interval,)):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, tuple):
        return [_jsonable(x) for x in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {
            k: _jsonable(v)
            for k, v in vars(obj).items()
            if not k.startswith("_")
        }
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, set)):
        return [_jsonable(x) for x in obj]
    return obj


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    """Defaults for one study run (settings-driven in production)."""

    intervals: tuple[Interval, ...] = (
        Interval.D1,
        Interval.H4,
        Interval.H1,
        Interval.M30,
        Interval.M15,
        Interval.M5,
        Interval.M1,
    )
    lookback_days: int = 730
    signal: SignalParams = SignalParams()
    sim: SimParams = SimParams()
    param_grid: tuple[SignalParams, ...] = (
        SignalParams(mode="trend", fast=5, slow=21, band=0.0),
        SignalParams(mode="trend", fast=9, slow=21, band=0.0),
        SignalParams(mode="trend", fast=9, slow=50, band=0.0),
        SignalParams(mode="trend", fast=9, slow=21, band=0.001),
        SignalParams(mode="trend", fast=15, slow=50, band=0.0),
        SignalParams(mode="reversion"),
    )
    n_folds: int = 4
    min_train_bars: int = 100
    min_coverage_pct: float = 0.9
    combination_mode: str = "all"
    max_symbols: int = 20


class MultitimeframeResearchEngine:
    """Loads bars and produces the multi-timeframe research report.

    Only this class performs I/O (price/stock/universe repositories). All
    analysis is delegated to the pure functions above.
    """

    def __init__(
        self,
        *,
        prices: PriceRepository,
        stocks: StockRepository,
        universe: UniverseRepository | None = None,
        settings: ResearchSettings | None = None,
        logger: Any | None = None,
    ) -> None:
        self._prices = prices
        self._stocks = stocks
        self._universe = universe
        self._settings = settings or ResearchSettings()
        self._logger = logger

    async def run(
        self,
        *,
        symbols: Iterable[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> ResearchReport:
        as_of = datetime.now(UTC).date()
        end = end or as_of
        start = start or (end - timedelta(days=self._settings.lookback_days))
        resolved = await self._resolve_symbols(symbols)
        if not resolved:
            return ResearchReport(
                as_of=as_of, symbols=(), start=start, end=end,
                timeframe_studies=[], combinations=[], recommendations=[],
                best_context=self._settings.intervals[0],
                best_setup=(
                    self._settings.intervals[1]
                    if len(self._settings.intervals) > 1
                    else self._settings.intervals[0]
                ),
                best_entry=self._settings.intervals[-1],
                limitations=("no symbols resolved",),
            )

        bars_by_symbol: dict[str, dict[Interval, list[PriceBar]]] = {}
        for symbol in resolved:
            bars_by_symbol[symbol] = await self._load_intervals(symbol, start, end)

        # 1. Single-timeframe analysis.
        timeframe_studies = self._timeframe_studies(bars_by_symbol)

        # 2. Combination study.
        combinations = await self._combination_studies(bars_by_symbol, start, end)

        # 3. Recommendation.
        recommendations = rank_recommendations(combinations)
        best_context, best_setup, best_entry = best_roles(recommendations)

        limitations = self._limitations(bars_by_symbol)
        return ResearchReport(
            as_of=as_of,
            symbols=tuple(resolved),
            start=start,
            end=end,
            timeframe_studies=timeframe_studies,
            combinations=combinations,
            recommendations=recommendations,
            best_context=best_context,
            best_setup=best_setup,
            best_entry=best_entry,
            limitations=tuple(limitations),
        )

    async def _resolve_symbols(
        self, symbols: Iterable[str] | None
    ) -> list[str]:
        if symbols is not None:
            return list(symbols)[: self._settings.max_symbols]
        if self._universe is not None:
            try:
                memberships = await self._universe.list_memberships()
                return [
                    m.symbol
                    for m in memberships
                    if m.status.value == "active"
                ][: self._settings.max_symbols]
            except Exception:
                pass
        stocks = await self._stocks.list_active()
        return [s.symbol for s in stocks][: self._settings.max_symbols]

    async def _load_intervals(
        self, symbol: str, start: date, end: date
    ) -> dict[Interval, list[PriceBar]]:
        out: dict[Interval, list[PriceBar]] = {}
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(end, time.max, tzinfo=UTC)
        for interval in self._settings.intervals:
            source = derived_source(interval)
            if source is not None:
                source_bars = await self._prices.history(
                    symbol, source, start_dt, end_dt, limit=100_000
                )
                out[interval] = resample_bars(
                    source_bars, target=interval, source=source
                )
            else:
                out[interval] = await self._prices.history(
                    symbol, interval, start_dt, end_dt, limit=100_000
                )
        return out

    def _timeframe_studies(
        self, bars_by_symbol: dict[str, dict[Interval, list[PriceBar]]]
    ) -> list[TimeframeStudy]:
        studies: list[TimeframeStudy] = []
        for interval in self._settings.intervals:
            symbol_bars = {
                s: b[interval]
                for s, b in bars_by_symbol.items()
                if b.get(interval)
            }
            if not symbol_bars:
                continue
            # Representative study on the symbol with the most bars.
            symbol = max(symbol_bars, key=lambda s: len(symbol_bars[s]))
            study = analyze_timeframe(
                symbol_bars[symbol],
                interval,
                params=self._settings.signal,
                sim_params=self._settings.sim,
                min_coverage_pct=self._settings.min_coverage_pct,
            )
            studies.append(study)
        return studies

    async def _combination_studies(
        self,
        bars_by_symbol: dict[str, dict[Interval, list[PriceBar]]],
        start: date,
        end: date,
    ) -> list[CombinationResult]:
        combos = enumerate_combos(self._settings.intervals)
        results: list[CombinationResult] = []
        for combo in combos:
            eligible = {
                s: b for s, b in bars_by_symbol.items()
                if combo.entry in b
                and combo.setup in b
                and combo.context in b
                and b[combo.entry]
                and b[combo.setup]
                and b[combo.context]
            }
            if not eligible:
                continue
            per_symbol: dict[str, StudyMetrics] = {}
            wf_summaries: list[WalkForwardSummary] = []
            ps_list: list[ParamSensitivity] = []
            cost_points: list[CostPoint] | None = None
            for symbol, b in eligible.items():
                regime = regime_labels_for(b.get(Interval.D1, []))
                metrics = _combo_metrics(
                    b[combo.entry],
                    b,
                    combo,
                    params=self._settings.signal,
                    sim_params=self._settings.sim,
                    mode=self._settings.combination_mode,
                    regime=regime,
                )
                per_symbol[symbol] = metrics
                wf = walk_forward(
                    b[combo.entry],
                    b,
                    combo,
                    self._settings.param_grid,
                    sim_params=self._settings.sim,
                    n_folds=self._settings.n_folds,
                    min_train_bars=self._settings.min_train_bars,
                    mode=self._settings.combination_mode,
                    regime=regime,
                )
                wf_summaries.append(wf)
                ps = parameter_sensitivity(
                    b[combo.entry],
                    b,
                    combo,
                    self._settings.param_grid,
                    sim_params=self._settings.sim,
                    mode=self._settings.combination_mode,
                )
                ps_list.append(ps)
                if cost_points is None:
                    target = signal_series(b[combo.entry], self._settings.signal)
                    cost_points = cost_sweep(
                        b[combo.entry], target, params=self._settings.sim
                    )

            aggregated_wf = _aggregate_wf(wf_summaries)
            aggregated_ps = _aggregate_ps(ps_list)
            results.append(
                CombinationResult(
                    combo=combo,
                    metrics=aggregate_metrics(per_symbol),
                    per_symbol=per_symbol,
                    cost_sensitivity=cost_points or [],
                    walk_forward=aggregated_wf,
                    param_sensitivity=aggregated_ps,
                )
            )
        return results

    def _limitations(
        self, bars_by_symbol: dict[str, dict[Interval, list[PriceBar]]]
    ) -> list[str]:
        limitations: list[str] = []
        for interval in self._settings.intervals:
            available = sum(
                1
                for b in bars_by_symbol.values()
                if b.get(interval)
            )
            if available == 0:
                limitations.append(
                    f"{interval.value}: no persisted data available in window"
                )
            elif available < len(bars_by_symbol):
                limitations.append(
                    f"{interval.value}: partial coverage "
                    f"({available}/{len(bars_by_symbol)} symbols)"
                )
        return limitations


def _aggregate_wf(summaries: Sequence[WalkForwardSummary]) -> WalkForwardSummary:
    valid = [s for s in summaries if s.n_folds > 0]
    if not valid:
        return WalkForwardSummary(
            n_folds=0, oos_sharpe_mean=0.0, oos_sharpe_positive_ratio=0.0,
            oos_return_mean=0.0, oos_return_positive_ratio=0.0, oos_trades=0,
        )
    sharpes = [s.oos_sharpe_mean for s in valid]
    returns = [s.oos_return_mean for s in valid]
    return WalkForwardSummary(
        n_folds=sum(s.n_folds for s in valid),
        oos_sharpe_mean=round(statistics.fmean(sharpes), 4),
        oos_sharpe_positive_ratio=round(
            sum(1 for s in valid if s.oos_sharpe_mean > 0) / len(valid), 4
        ),
        oos_return_mean=round(statistics.fmean(returns), 4),
        oos_return_positive_ratio=round(
            sum(1 for s in valid if s.oos_return_mean > 0) / len(valid), 4
        ),
        oos_trades=sum(s.oos_trades for s in valid),
    )


def _aggregate_ps(ps_list: Sequence[ParamSensitivity]) -> ParamSensitivity:
    valid = list(ps_list)
    if not valid:
        return ParamSensitivity(
            grid_size=0, sharpe_mean=0.0, sharpe_std=0.0,
            sharpe_positive_ratio=0.0, max_drawdown_mean=0.0,
            best_params=SignalParams(), best_sharpe=0.0,
        )
    return ParamSensitivity(
        grid_size=valid[0].grid_size,
        sharpe_mean=round(
            statistics.fmean(p.sharpe_mean for p in valid), 4
        ),
        sharpe_std=round(
            statistics.fmean(p.sharpe_std for p in valid), 4
        ),
        sharpe_positive_ratio=round(
            statistics.fmean(p.sharpe_positive_ratio for p in valid), 4
        ),
        max_drawdown_mean=round(
            statistics.fmean(p.max_drawdown_mean for p in valid), 4
        ),
        best_params=max(valid, key=lambda p: p.best_sharpe).best_params,
        best_sharpe=round(max(p.best_sharpe for p in valid), 4),
    )


__all__ = [
    "BARS_PER_DAY",
    "BARS_PER_YEAR",
    "CombinationResult",
    "CostPoint",
    "DayRegime",
    "ParamSensitivity",
    "Recommendation",
    "RegimeSlice",
    "ResearchReport",
    "ResearchSettings",
    "ResearchTrade",
    "SimParams",
    "SimResult",
    "SignalParams",
    "StudyMetrics",
    "TimeframeCombo",
    "TimeframeQuality",
    "TimeframeStudy",
    "WalkForwardSummary",
    "WfFold",
    "MultitimeframeResearchEngine",
    "aggregate_metrics",
    "align_latest",
    "analyze_timeframe",
    "best_roles",
    "combine_signals",
    "compute_study_metrics",
    "cost_sweep",
    "enumerate_combos",
    "parameter_sensitivity",
    "rank_recommendations",
    "regime_labels_for",
    "resample_bars",
    "signal_series",
    "simulate",
    "timeframe_quality",
    "walk_forward",
]
