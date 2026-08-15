"""Decision ledger — the reproducible audit trail for every AI decision.

Each decision is stored as an immutable :class:`AiDecisionRecord` containing
the full trace: agent signals (with versions), sentiment, market regime,
timeframes, confidence, expected return/risk, the proposed size, the Phase 5
risk verdict, the simulated-execution outcome and any AI failure events.
Records round-trip through JSON-lines so runs can be replayed identically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from qtrader.application.ai.models import (
    AiDecisionRecord,
    AssetContext,
    DecisionProposal,
    ExecutionOutcome,
    FailureEvent,
    RiskGateResult,
    StrategySelection,
)


def build_decision_record(
    *,
    decision_id: str,
    timestamp: datetime,
    asset: AssetContext,
    strategy: StrategySelection,
    agents_involved: tuple[str, ...],
    agent_signals: dict[str, dict[str, Any]],
    sentiment: dict[str, Any] | None,
    market_regime: dict[str, Any] | None,
    timeframes: tuple[str, ...],
    confidence: float | None,
    expected_return: float | None,
    expected_risk: float | None,
    proposed_position_size: Decimal | None,
    risk: RiskGateResult | None = None,
    execution: ExecutionOutcome | None = None,
    failures: tuple[FailureEvent, ...] = (),
    proposal: DecisionProposal | None = None,
) -> AiDecisionRecord:
    """Assemble a record from the coordinator's trace pieces (auditable)."""
    if risk is None:
        risk_approval = "not_gated"
        risk_reason = "not_routed_to_risk_engine"
    else:
        risk_approval = (
            "approved"
            if risk.approved
            else ("capped" if risk.verdict.value == "modify" else "rejected")
        )
        risk_reason = " ".join(risk.reasons)
    return AiDecisionRecord(
        decision_id=decision_id,
        timestamp=timestamp,
        asset=asset.symbol,
        strategy=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        agents_involved=agents_involved,
        agent_signals=agent_signals,
        sentiment=sentiment,
        market_regime=market_regime,
        timeframes=timeframes,
        confidence=confidence,
        expected_return=expected_return,
        expected_risk=expected_risk,
        proposed_position_size=proposed_position_size,
        risk_approval=risk_approval,
        risk_reason=risk_reason,
        execution_assumptions=(
            {
                "scenario": execution.assumptions.scenario,
                "commission_bps": execution.assumptions.commission_bps,
                "slippage_bps": execution.assumptions.slippage_bps,
                "max_participation_rate": execution.assumptions.max_participation_rate,
                "seed": execution.assumptions.seed,
            }
            if execution
            else None
        ),
        execution_result=execution.to_dict() if execution else None,
        failure_events=tuple(f.code for f in failures),
        proposal=proposal.to_dict() if proposal else None,
    )


class DecisionLedger:
    """In-memory decision store with optional JSON-lines persistence."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._records: dict[str, AiDecisionRecord] = {}
        if self._path is not None and self._path.exists():
            self.load(self._path)

    def record(self, record: AiDecisionRecord) -> None:
        self._records[record.decision_id] = record

    def get(self, decision_id: str) -> AiDecisionRecord | None:
        return self._records.get(decision_id)

    def all(self, limit: int | None = None) -> tuple[AiDecisionRecord, ...]:
        ordered = sorted(self._records.values(), key=lambda r: r.timestamp)
        if limit is not None:
            ordered = ordered[-limit:]
        return tuple(ordered)

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
                data = json.loads(line)
                self._records[data["decision_id"]] = AiDecisionRecord.from_dict(data)
                loaded += 1
        return loaded


@dataclass(frozen=True, slots=True)
class LedgerStats:
    total: int
    proposed: int
    approved: int
    rejected: int
    capped: int
    degraded: int
    earliest: datetime | None
    latest: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "proposed": self.proposed,
            "approved": self.approved,
            "rejected": self.rejected,
            "capped": self.capped,
            "degraded": self.degraded,
            "earliest": self.earliest.isoformat() if self.earliest else None,
            "latest": self.latest.isoformat() if self.latest else None,
        }


def ledger_stats(ledger: DecisionLedger) -> LedgerStats:
    """Aggregate counts for reporting — the audit trail summary."""
    records = ledger.all()
    proposed = sum(1 for r in records if r.proposal is not None)
    approved = sum(1 for r in records if r.risk_approval == "approved")
    rejected = sum(1 for r in records if r.risk_approval == "rejected")
    capped = sum(1 for r in records if r.risk_approval == "capped")
    not_gated = sum(1 for r in records if r.risk_approval == "not_gated")
    times = [r.timestamp for r in records]
    return LedgerStats(
        total=len(records),
        proposed=proposed,
        approved=approved,
        rejected=rejected,
        capped=capped,
        degraded=not_gated,
        earliest=min(times) if times else None,
        latest=max(times) if times else None,
    )


__all__ = ["DecisionLedger", "LedgerStats", "build_decision_record", "ledger_stats"]
