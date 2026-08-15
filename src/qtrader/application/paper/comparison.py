"""Paper vs research comparison (required output #5).

Compares what the paper account actually traded against the research evidence
(backtest / walk-forward / execution-aware simulation): returns, slippage, fill
rates, trade frequency, drawdown, strategy selection and agent signals. The
output is a structured :class:`ComparisonReport` whose rows feed both the audit
document and the acceptance evaluator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from qtrader.application.paper.ledger import PaperOrderLedger
from qtrader.application.paper.models import (
    PaperOrderRecord,
    PaperOrderStatus,
)
from qtrader.domain.entities import PerformanceSummary, Signal, Trade


@dataclass(frozen=True, slots=True)
class ComparisonInput:
    """Everything the comparator needs (assembled from repos by the service)."""

    paper_records: tuple[PaperOrderRecord, ...] = ()
    paper_trades: tuple[Trade, ...] = ()
    research_summary: PerformanceSummary | None = None
    research_signals: tuple[Signal, ...] = ()
    research_fill_rate: float | None = None
    initial_capital: Decimal = Decimal("100000")


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    dimension: str
    paper_value: float | None
    research_value: float | None
    divergence: float | None
    interpretation: str


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    rows: tuple[ComparisonRow, ...]
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "rows": [
                {
                    "dimension": row.dimension,
                    "paper_value": row.paper_value,
                    "research_value": row.research_value,
                    "divergence": row.divergence,
                    "interpretation": row.interpretation,
                }
                for row in self.rows
            ],
        }


def _pct(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _slippage_bps(records: tuple[PaperOrderRecord, ...]) -> float | None:
    values: list[float] = []
    for record in records:
        if (
            record.slippage is not None
            and record.fill_price is not None
            and record.fill_price != 0
        ):
            values.append(float(record.slippage / record.fill_price * Decimal("10000")))
    return sum(values) / len(values) if values else None


def _fill_rate(records: tuple[PaperOrderRecord, ...]) -> float | None:
    executed = [
        r
        for r in records
        if r.status in (PaperOrderStatus.FILLED, PaperOrderStatus.PARTIAL)
    ]
    attempted = [
        r
        for r in records
        if r.status
        in (PaperOrderStatus.FILLED, PaperOrderStatus.PARTIAL, PaperOrderStatus.REJECTED)
    ]
    return len(executed) / len(attempted) if attempted else None


def _max_drawdown(trades: tuple[Trade, ...]) -> float | None:
    equity = Decimal("0")
    peak = Decimal("0")
    worst = Decimal("0")
    for trade in trades:
        equity += trade.pnl or Decimal("0")
        peak = max(peak, equity)
        if peak > 0:
            drawdown = (peak - equity) / peak
            worst = min(worst, drawdown)
    return float(worst) if trades else None


def _trades_per_day(trades: tuple[Trade, ...]) -> float | None:
    if not trades:
        return None
    first = min(t.entry_time for t in trades)
    last = max(t.exit_time for t in trades)
    days = (last - first).total_seconds() / 86400.0
    span = max(days, 1.0)
    return len(trades) / span


def _frequency_signals(signals: tuple[Signal, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for signal in signals:
        counts[signal.agent] = counts.get(signal.agent, 0) + 1
    return counts


def _paper_signals(records: tuple[PaperOrderRecord, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        agent = record.context.get("agent")
        if agent:
            counts[str(agent)] = counts.get(str(agent), 0) + 1
    return counts


class PaperVsResearchComparator:
    """Pure comparator; call :meth:`compare` with :class:`ComparisonInput`."""

    def compare(self, data: ComparisonInput) -> ComparisonReport:
        rows: list[ComparisonRow] = []

        paper_pnl = sum((t.pnl or Decimal("0") for t in data.paper_trades), Decimal("0"))
        paper_return = None
        if data.paper_trades and data.initial_capital:
            paper_return = float(paper_pnl / data.initial_capital)
        research_return = (
            _pct(data.research_summary.total_return) if data.research_summary else None
        )
        rows.append(
            ComparisonRow(
                dimension="total_return",
                paper_value=paper_return,
                research_value=research_return,
                divergence=_divergence(paper_return, research_return),
                interpretation="paper PnL vs research total return",
            )
        )

        rows.append(
            ComparisonRow(
                dimension="avg_slippage_bps",
                paper_value=_slippage_bps(data.paper_records),
                research_value=None,
                divergence=None,
                interpretation="paper execution slippage (fill vs requested)",
            )
        )

        fill_rate = _fill_rate(data.paper_records)
        rows.append(
            ComparisonRow(
                dimension="fill_rate",
                paper_value=fill_rate,
                research_value=data.research_fill_rate,
                divergence=_divergence(fill_rate, data.research_fill_rate),
                interpretation="paper fills vs execution-aware simulation assumption",
            )
        )

        rows.append(
            ComparisonRow(
                dimension="trade_frequency_per_day",
                paper_value=_trades_per_day(data.paper_trades),
                research_value=(
                    float(data.research_summary.trades_count) / 252.0
                    if data.research_summary and data.research_summary.trades_count
                    else None
                ),
                divergence=None,
                interpretation="paper trade cadence vs research",
            )
        )

        paper_dd = _max_drawdown(data.paper_trades)
        research_dd = (
            _pct(data.research_summary.max_drawdown) if data.research_summary else None
        )
        rows.append(
            ComparisonRow(
                dimension="max_drawdown",
                paper_value=paper_dd,
                research_value=research_dd,
                divergence=_divergence(paper_dd, research_dd),
                interpretation="paper equity drawdown vs research",
            )
        )

        paper_strategies = sorted({r.strategy for r in data.paper_records})
        research_strategies = (
            [data.research_summary.strategy] if data.research_summary else []
        )
        rows.append(
            ComparisonRow(
                dimension="strategy_selection",
                paper_value=float(len(paper_strategies)),
                research_value=float(len(research_strategies)) if research_strategies else None,
                divergence=(
                    float(len(set(paper_strategies) - set(research_strategies)))
                    if research_strategies
                    else None
                ),
                interpretation="strategies selected in paper vs research",
            )
        )

        paper_signal_counts = _paper_signals(data.paper_records)
        research_signal_counts = _frequency_signals(data.research_signals)
        rows.append(
            ComparisonRow(
                dimension="agent_signal_frequency",
                paper_value=float(len(paper_signal_counts)),
                research_value=float(len(research_signal_counts)),
                divergence=None,
                interpretation="distinct contributing agents in paper vs research",
            )
        )

        return ComparisonReport(rows=tuple(rows))

    async def from_repositories(
        self,
        *,
        ledger: PaperOrderLedger,
        trades: tuple[Trade, ...],
        research_summary: PerformanceSummary | None,
        research_signals: tuple[Signal, ...],
        research_fill_rate: float | None = None,
        initial_capital: Decimal = Decimal("100000"),
    ) -> ComparisonReport:
        """Convenience wrapper keeping the comparator free of repository deps."""
        return self.compare(
            ComparisonInput(
                paper_records=ledger.all(),
                paper_trades=trades,
                research_summary=research_summary,
                research_signals=research_signals,
                research_fill_rate=research_fill_rate,
                initial_capital=initial_capital,
            )
        )


def _divergence(paper: float | None, research: float | None) -> float | None:
    if paper is None or research is None:
        return None
    if research == 0:
        return abs(paper) if paper else 0.0
    return abs(paper - research) / abs(research)


__all__ = ["ComparisonInput", "ComparisonReport", "ComparisonRow", "PaperVsResearchComparator"]
