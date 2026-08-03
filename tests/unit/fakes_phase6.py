"""Shared fakes for Phase 6 tests (not collected by pytest)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from qtrader.domain.entities import BacktestRun, PerformanceSummary, SystemLog
from qtrader.domain.ports import (
    BacktestRepository,
    PerformanceRepository,
    PriceRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import Money, PriceBar, TradingMode


class FakePriceRepository(PriceRepository):
    def __init__(self, bars: dict[str, list[PriceBar]] | None = None) -> None:
        self._bars: dict[str, list[PriceBar]] = bars or {}

    async def upsert_bars(self, bars: list[PriceBar]) -> int:
        for bar in bars:
            self._bars.setdefault(bar.symbol, []).append(bar)
        return len(bars)

    async def latest(self, symbol: str, interval) -> PriceBar | None:
        bars = self._bars.get(symbol) or []
        return bars[-1] if bars else None

    async def history(self, symbol, interval, start=None, end=None, limit=500) -> list[PriceBar]:
        bars = [b for b in self._bars.get(symbol, []) if b.interval is interval]
        if start is not None:
            bars = [b for b in bars if b.ts >= start]
        if end is not None:
            bars = [b for b in bars if b.ts <= end]
        return bars[:limit]


class FakeBacktestRepository(BacktestRepository):
    def __init__(self) -> None:
        self.runs: list[BacktestRun] = []
        self._next = 1

    async def create(self, run: BacktestRun) -> BacktestRun:
        run = replace(run, run_id=self._next)
        self._next += 1
        self.runs.append(run)
        return run

    async def save(self, run: BacktestRun) -> BacktestRun:
        self.runs = [r if r.run_id != run.run_id else run for r in self.runs]
        return run

    async def get(self, run_id: int) -> BacktestRun | None:
        for run in self.runs:
            if run.run_id == run_id:
                return run
        return None

    async def latest(self, name: str | None = None, limit: int = 5) -> list[BacktestRun]:
        runs = list(self.runs)
        if name is not None:
            runs = [r for r in runs if r.name == name]
        return runs[:limit]


class FakePerformanceRepository(PerformanceRepository):
    def __init__(self) -> None:
        self.summaries: list[PerformanceSummary] = []

    async def upsert(self, summary: PerformanceSummary) -> PerformanceSummary:
        self.summaries = [
            s
            for s in self.summaries
            if not (
                s.strategy == summary.strategy
                and s.mode is summary.mode
                and s.period_start == summary.period_start
                and s.period_end == summary.period_end
            )
        ]
        self.summaries.append(summary)
        return summary

    async def latest_for_strategy(
        self, strategy: str, mode: TradingMode
    ) -> PerformanceSummary | None:
        matches = [s for s in self.summaries if s.strategy == strategy and s.mode is mode]
        return matches[-1] if matches else None


class FakeSystemLogRepository(SystemLogRepository):
    def __init__(self) -> None:
        self.entries: list[SystemLog] = []

    async def record(self, entry: SystemLog) -> SystemLog:
        entry = replace(entry, log_id=len(self.entries) + 1)
        self.entries.append(entry)
        return entry

    async def recent(
        self, level: str | None = None, component: str | None = None, limit: int = 50
    ) -> list[SystemLog]:
        entries = self.entries
        if level is not None:
            entries = [e for e in entries if e.level == level.upper()]
        if component is not None:
            entries = [e for e in entries if e.component == component]
        return list(reversed(entries))[:limit]


def money(value: str) -> Money:
    return Money(Decimal(value))


def bar(
    symbol: str,
    ts: datetime,
    open: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1000000",
    interval: object = None,
) -> PriceBar:
    from qtrader.domain.value_objects import Interval

    return PriceBar(
        symbol=symbol,
        interval=interval if interval is not None else Interval.D1,
        ts=ts,
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )
