"""Paper order ledger — append-only audit trail of paper executions.

Mirrors the Phase 6 :class:`DecisionLedger` pattern: an in-memory map keyed by
``PaperOrderRecord.key`` (the decision reference when present, otherwise the
record's own id) with optional JSON-lines persistence. Reloading an existing
ledger file after a process restart is how the recovery path guarantees it can
re-poll stale orders without ever duplicating them.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from qtrader.application.paper.models import (
    PaperOrderRecord,
    PaperOrderStatus,
    PaperRunStats,
)


class PaperOrderLedger:
    """In-memory paper ledger with optional JSON-lines persistence."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._records: dict[str, PaperOrderRecord] = {}
        if self._path is not None and self._path.exists():
            self.load(self._path)

    @property
    def path(self) -> Path | None:
        return self._path

    def record(self, record: PaperOrderRecord) -> None:
        """Insert or replace a record keyed by ``record.key``."""
        self._records[record.key] = record

    def update(self, key: str, **fields: Any) -> PaperOrderRecord:
        """Merge ``fields`` into the record at ``key`` (creates a stub if absent).

        Returns the merged record. Only non-``None`` values override existing
        fields so risk verdicts recorded before submission survive fill updates.
        """
        current = self._records.get(key)
        if current is None:
            current = PaperOrderRecord(key=key)
        updates = {name: value for name, value in fields.items() if value is not None}
        merged = replace(current, **updates)
        self._records[key] = merged
        return merged

    def get(self, key: str) -> PaperOrderRecord | None:
        return self._records.get(key)

    def get_by_decision_ref(self, decision_ref: str) -> PaperOrderRecord | None:
        for record in self._records.values():
            if record.decision_ref == decision_ref:
                return record
        return None

    def all(self, limit: int | None = None) -> tuple[PaperOrderRecord, ...]:
        ordered = sorted(self._records.values(), key=lambda r: r.timestamp)
        if limit is not None:
            ordered = ordered[-limit:]
        return tuple(ordered)

    def since(self, since: datetime) -> tuple[PaperOrderRecord, ...]:
        return tuple(r for r in self.all() if r.timestamp >= since)

    def stale(self) -> tuple[PaperOrderRecord, ...]:
        """Records awaiting a fill after a restart (SUBMITTED, never terminal)."""
        return tuple(
            r
            for r in self._records.values()
            if r.status is PaperOrderStatus.SUBMITTED and not r.is_terminal
        )

    def count(self) -> int:
        return len(self._records)

    def write(self, path: str | Path | None = None) -> int:
        """Persist all records as JSON-lines; returns the count written."""
        target = Path(path) if path else self._path
        if target is None:
            raise ValueError("no ledger path configured")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for record in self.all():
                handle.write(json.dumps(record.to_dict()) + "\n")
        return self.count()

    def load(self, path: str | Path | None = None) -> int:
        """Load records from a JSON-lines file; returns the count loaded."""
        target = Path(path) if path else self._path
        if target is None or not target.exists():
            return 0
        loaded = 0
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = PaperOrderRecord.from_dict(json.loads(line))
                self._records[record.key] = record
                loaded += 1
        return loaded

    def clear(self) -> None:
        self._records.clear()


def _to_bps(slippage: Decimal | None, price: Decimal | None) -> float | None:
    if slippage is None or price is None or price == 0:
        return None
    return float((slippage / price) * Decimal("10000"))


def ledger_stats(ledger: PaperOrderLedger) -> PaperRunStats:
    """Aggregate :class:`PaperRunStats` over all records (required output #2)."""
    records = ledger.all()
    statuses = [r.status for r in records]
    filled = statuses.count(PaperOrderStatus.FILLED)
    submitted = statuses.count(PaperOrderStatus.SUBMITTED)
    shadow = statuses.count(PaperOrderStatus.SHADOW_ONLY)
    attempted = filled + submitted
    fill_rate = filled / attempted if attempted else 0.0

    slippages: list[float] = []
    latencies: list[float] = []
    for record in records:
        if record.slippage is not None and record.fill_price is not None:
            bps = _to_bps(record.slippage, record.fill_price)
            if bps is not None:
                slippages.append(bps)
        if record.execution_latency_ms is not None:
            latencies.append(record.execution_latency_ms)

    verdicts = [r.risk_verdict for r in records]
    times = [r.timestamp for r in records]
    return PaperRunStats(
        total_orders=len(records),
        proposed=statuses.count(PaperOrderStatus.PROPOSED),
        submitted=submitted,
        filled=filled,
        partial=statuses.count(PaperOrderStatus.PARTIAL),
        canceled=statuses.count(PaperOrderStatus.CANCELED),
        rejected=statuses.count(PaperOrderStatus.REJECTED),
        shadow_only=shadow,
        fill_rate=fill_rate,
        avg_slippage_bps=sum(slippages) / len(slippages) if slippages else 0.0,
        avg_execution_latency_ms=(
            sum(latencies) / len(latencies) if latencies else 0.0
        ),
        total_commission=sum((r.commission for r in records), Decimal("0")),
        risk_approved=verdicts.count("approved"),
        risk_capped=verdicts.count("capped"),
        risk_rejected=verdicts.count("rejected"),
        risk_not_gated=verdicts.count(None),
        earliest=min(times) if times else None,
        latest=max(times) if times else None,
    )


__all__ = ["PaperOrderLedger", "ledger_stats"]
