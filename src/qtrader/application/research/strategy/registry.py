"""Strategy registry — store, version, enable/disable and compare hypotheses.

A :class:`StrategyRecord` binds a generated :class:`StrategySpec` to its research
outcome: status, universe/dataset/period used, the net-of-cost backtest metrics
and any robustness flags. The :class:`StrategyRegistry` port keeps experiments
reproducible (``export``/``import_`` round-trip every record) without coupling
the research engine to a persistence layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from qtrader.application.research.strategy.robustness import RobustnessReport
from qtrader.application.research.strategy.specs import StrategySpec, decode_spec, encode_spec
from qtrader.domain.entities import PerformanceSummary
from qtrader.domain.value_objects import TradingMode


def _now() -> datetime:
    return datetime.now(UTC)


class StrategyStatus(StrEnum):
    """Lifecycle state of a research strategy (validated is stage-gated)."""

    GENERATED = "generated"
    REJECTED = "rejected"
    INITIAL_BACKTEST = "initial_backtest"
    VALIDATED = "validated"
    FAILED = "failed"
    EXECUTION_REJECTED = "execution_rejected"
    EXECUTION_SENSITIVE = "execution_sensitive"
    EXECUTION_ROBUST = "execution_robust"


@dataclass(frozen=True, slots=True)
class StrategyRecord:
    """A registered strategy hypothesis with its research outcome."""

    spec: StrategySpec
    status: StrategyStatus = StrategyStatus.GENERATED
    universe: tuple[str, ...] = ()
    dataset_version: str = ""
    backtest_period: str = ""
    metrics: PerformanceSummary | None = None
    robustness: RobustnessReport | None = None
    enabled: bool = False
    created_at: datetime = field(default_factory=_now)
    notes: str = ""

    @property
    def strategy_id(self) -> str:
        return self.spec.id


class StrategyRegistry(ABC):
    """Persistence seam for strategy records (in-memory by default)."""

    @abstractmethod
    def register(
        self,
        spec: StrategySpec,
        *,
        status: StrategyStatus = StrategyStatus.GENERATED,
        note: str = "",
    ) -> StrategyRecord: ...

    @abstractmethod
    def get(self, strategy_id: str, version: int = 1) -> StrategyRecord | None: ...

    @abstractmethod
    def list_all(
        self,
        status: StrategyStatus | None = None,
        enabled: bool | None = None,
    ) -> list[StrategyRecord]: ...

    @abstractmethod
    def update(self, record: StrategyRecord) -> StrategyRecord: ...

    @abstractmethod
    def set_status(
        self,
        strategy_id: str,
        status: StrategyStatus,
        version: int = 1,
        note: str = "",
    ) -> StrategyRecord | None: ...

    @abstractmethod
    def set_enabled(
        self, strategy_id: str, enabled: bool, version: int = 1
    ) -> StrategyRecord | None: ...

    @abstractmethod
    def compare(self, strategy_ids: list[str]) -> list[StrategyRecord]: ...

    @abstractmethod
    def export(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def import_(self, payload: list[dict[str, Any]]) -> int: ...


class InMemoryStrategyRegistry(StrategyRegistry):
    """Thread-safe in-memory registry (research session scope)."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, int], StrategyRecord] = {}

    def register(
        self,
        spec: StrategySpec,
        *,
        status: StrategyStatus = StrategyStatus.GENERATED,
        note: str = "",
    ) -> StrategyRecord:
        record = StrategyRecord(spec=spec, status=status, notes=note)
        self._records[(spec.id, spec.version)] = record
        return record

    def get(self, strategy_id: str, version: int = 1) -> StrategyRecord | None:
        return self._records.get((strategy_id, version))

    def list_all(
        self,
        status: StrategyStatus | None = None,
        enabled: bool | None = None,
    ) -> list[StrategyRecord]:
        out = []
        for record in self._records.values():
            if status is not None and record.status is not status:
                continue
            if enabled is not None and record.enabled is not enabled:
                continue
            out.append(record)
        return sorted(out, key=lambda r: (r.created_at, r.strategy_id))

    def update(self, record: StrategyRecord) -> StrategyRecord:
        key = (record.spec.id, record.spec.version)
        if key not in self._records:
            raise KeyError(f"unknown strategy {record.spec.id} v{record.spec.version}")
        self._records[key] = record
        return record

    def set_status(
        self,
        strategy_id: str,
        status: StrategyStatus,
        version: int = 1,
        note: str = "",
    ) -> StrategyRecord | None:
        key = (strategy_id, version)
        record = self._records.get(key)
        if record is None:
            return None
        updated = StrategyRecord(
            spec=record.spec,
            status=status,
            universe=record.universe,
            dataset_version=record.dataset_version,
            backtest_period=record.backtest_period,
            metrics=record.metrics,
            robustness=record.robustness,
            enabled=record.enabled,
            created_at=record.created_at,
            notes=note or record.notes,
        )
        self._records[key] = updated
        return updated

    def set_enabled(
        self, strategy_id: str, enabled: bool, version: int = 1
    ) -> StrategyRecord | None:
        key = (strategy_id, version)
        record = self._records.get(key)
        if record is None:
            return None
        updated = StrategyRecord(
            spec=record.spec,
            status=record.status,
            universe=record.universe,
            dataset_version=record.dataset_version,
            backtest_period=record.backtest_period,
            metrics=record.metrics,
            robustness=record.robustness,
            enabled=enabled,
            created_at=record.created_at,
            notes=record.notes,
        )
        self._records[key] = updated
        return updated

    def compare(self, strategy_ids: list[str]) -> list[StrategyRecord]:
        found: list[StrategyRecord] = []
        for strategy_id in strategy_ids:
            for version in (1,):
                record = self.get(strategy_id, version)
                if record is not None:
                    found.append(record)
        return found

    def export(self) -> list[dict[str, Any]]:
        payload = []
        for record in sorted(
            self._records.values(), key=lambda r: (r.created_at, r.strategy_id)
        ):
            metrics = None
            if record.metrics is not None:
                m = record.metrics
                metrics = {
                    "strategy": m.strategy,
                    "mode": m.mode.value,
                    "period_start": m.period_start.isoformat(),
                    "period_end": m.period_end.isoformat(),
                    "total_return": str(m.total_return),
                    "sharpe": str(m.sharpe),
                    "sortino": str(m.sortino),
                    "max_drawdown": str(m.max_drawdown),
                    "win_rate": str(m.win_rate),
                    "profit_factor": str(m.profit_factor),
                    "expectancy": str(m.expectancy),
                    "avg_win": str(m.avg_win),
                    "avg_loss": str(m.avg_loss),
                    "turnover": str(m.turnover),
                    "total_costs": str(m.total_costs),
                    "trades_count": m.trades_count,
                }
            payload.append(
                {
                    "spec": encode_spec(record.spec),
                    "status": record.status.value,
                    "universe": list(record.universe),
                    "dataset_version": record.dataset_version,
                    "backtest_period": record.backtest_period,
                    "metrics": metrics,
                    "robustness": (
                        record.robustness.to_dict() if record.robustness is not None else None
                    ),
                    "enabled": record.enabled,
                    "created_at": record.created_at.isoformat(),
                    "notes": record.notes,
                }
            )
        return payload

    def import_(self, payload: list[dict[str, Any]]) -> int:
        count = 0
        for item in payload:
            spec = decode_spec(item["spec"])
            record = StrategyRecord(
                spec=spec,
                status=StrategyStatus(item["status"]),
                universe=tuple(item.get("universe", ())),
                dataset_version=item.get("dataset_version", ""),
                backtest_period=item.get("backtest_period", ""),
                metrics=_decode_metrics(item.get("metrics")),
                robustness=None,
                enabled=bool(item.get("enabled", False)),
                created_at=datetime.fromisoformat(item["created_at"]),
                notes=item.get("notes", ""),
            )
            self._records[(spec.id, spec.version)] = record
            count += 1
        return count


def _decode_metrics(data: dict[str, Any] | None) -> PerformanceSummary | None:
    if data is None:
        return None
    from datetime import date as _date

    return PerformanceSummary(
        strategy=str(data.get("strategy", "")),
        mode=TradingMode(str(data.get("mode", "backtest"))),
        period_start=_date.fromisoformat(data["period_start"]),
        period_end=_date.fromisoformat(data["period_end"]),
        total_return=_dec(data.get("total_return")),
        sharpe=_dec(data.get("sharpe")),
        sortino=_dec(data.get("sortino")),
        max_drawdown=_dec(data.get("max_drawdown")),
        win_rate=_dec(data.get("win_rate")),
        profit_factor=_dec(data.get("profit_factor")),
        expectancy=_dec(data.get("expectancy")),
        avg_win=_dec(data.get("avg_win")),
        avg_loss=_dec(data.get("avg_loss")),
        turnover=_dec(data.get("turnover")),
        total_costs=_dec(data.get("total_costs")),
        trades_count=int(data["trades_count"]) if data.get("trades_count") is not None else None,
    )


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


__all__ = [
    "InMemoryStrategyRegistry",
    "StrategyRecord",
    "StrategyRegistry",
    "StrategyStatus",
]
