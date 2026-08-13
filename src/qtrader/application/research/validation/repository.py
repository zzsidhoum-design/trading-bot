"""Research database — every generated and tested strategy, fully reproducible.

The repository stores one :class:`ValidationRecord` per hypothesis: the spec,
the stage results, the robustness dimensions, the multiple-testing correction
and the final status. ``export``/``import_`` round-trip the entire database to
JSON so any experiment can be replayed from the stored record alone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from qtrader.application.research.validation.records import (
    ValidationRecord,
    decode_record,
    encode_record,
)


class ValidationRepository(ABC):
    """Persistence seam for per-strategy validation records."""

    @abstractmethod
    def register(self, record: ValidationRecord) -> ValidationRecord: ...

    @abstractmethod
    def get(self, strategy_id: str) -> ValidationRecord | None: ...

    @abstractmethod
    def list_all(self) -> list[ValidationRecord]: ...

    @abstractmethod
    def update(self, record: ValidationRecord) -> ValidationRecord: ...

    @abstractmethod
    def export(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def import_(self, payload: list[dict[str, Any]]) -> int: ...


class InMemoryValidationRepository(ValidationRepository):
    """Thread-safe in-memory research database (research session scope)."""

    def __init__(self) -> None:
        self._records: dict[str, ValidationRecord] = {}

    def register(self, record: ValidationRecord) -> ValidationRecord:
        if record.strategy_id in self._records:
            raise KeyError(f"strategy {record.strategy_id} already registered")
        self._records[record.strategy_id] = record
        return record

    def get(self, strategy_id: str) -> ValidationRecord | None:
        return self._records.get(strategy_id)

    def list_all(self) -> list[ValidationRecord]:
        return sorted(
            self._records.values(),
            key=lambda record: (record.created_at, record.strategy_id),
        )

    def update(self, record: ValidationRecord) -> ValidationRecord:
        if record.strategy_id not in self._records:
            raise KeyError(f"unknown strategy {record.strategy_id}")
        self._records[record.strategy_id] = record
        return record

    def export(self) -> list[dict[str, Any]]:
        return [encode_record(record) for record in self.list_all()]

    def import_(self, payload: list[dict[str, Any]]) -> int:
        count = 0
        for item in payload:
            record = decode_record(item)
            self._records[record.strategy_id] = record
            count += 1
        return count


__all__ = ["InMemoryValidationRepository", "ValidationRepository"]
