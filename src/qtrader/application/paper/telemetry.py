"""Operational telemetry — continuous reliability + latency measurements.

Recorders never raise: a telemetry failure must not break the trading loop.
Measurements land in ``agent_metrics`` (latencies, failure counters, signal
frequency) and ``system_logs`` (reconnection / failure events) so the
dashboard and the acceptance evaluator can read them back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from qtrader.config.logging import get_logger
from qtrader.domain.entities import AgentMetric, SystemLog
from qtrader.domain.ports import AgentMetricRepository, SystemLogRepository

logger = get_logger("qtrader.paper.telemetry")

_METRIC_AGENT = "paper"
_WINDOW = "paper"


class TelemetryRecorder(Protocol):
    """Write-only telemetry sink used by the paper layer."""

    async def record_metric(
        self,
        metric_name: str,
        value: float | Decimal,
        *,
        window: str = _WINDOW,
    ) -> None: ...

    async def record_log(
        self,
        level: str,
        message: str,
        *,
        component: str | None = None,
        context: dict | None = None,
    ) -> None: ...

    async def latency(self, stage: str, milliseconds: float) -> None: ...

    async def signal_frequency(self, agent: str, count: int = 1) -> None: ...

    async def api_failure(self, provider: str, error: str) -> None: ...

    async def missing_data(self, symbol: str, field_name: str) -> None: ...

    async def invalid_data(self, symbol: str, reason: str) -> None: ...

    async def reconnection(self, provider: str) -> None: ...


class NullTelemetry:
    """No-op recorder for backtest mode and unit tests."""

    async def record_metric(
        self,
        metric_name: str,
        value: float | Decimal,
        *,
        window: str = _WINDOW,
    ) -> None:
        return None

    async def record_log(
        self,
        level: str,
        message: str,
        *,
        component: str | None = None,
        context: dict | None = None,
    ) -> None:
        return None

    async def latency(self, stage: str, milliseconds: float) -> None:
        return None

    async def signal_frequency(self, agent: str, count: int = 1) -> None:
        return None

    async def api_failure(self, provider: str, error: str) -> None:
        return None

    async def missing_data(self, symbol: str, field_name: str) -> None:
        return None

    async def invalid_data(self, symbol: str, reason: str) -> None:
        return None

    async def reconnection(self, provider: str) -> None:
        return None


class OperationalTelemetry:
    """Persists telemetry into ``agent_metrics`` / ``system_logs``."""

    def __init__(
        self,
        agent_metrics: AgentMetricRepository,
        logs: SystemLogRepository,
    ) -> None:
        self._metrics = agent_metrics
        self._logs = logs

    async def record_metric(
        self,
        metric_name: str,
        value: float | Decimal,
        *,
        window: str = _WINDOW,
    ) -> None:
        try:
            await self._metrics.record(
                AgentMetric(
                    agent_name=_METRIC_AGENT,
                    metric_name=metric_name,
                    value=Decimal(str(value)),
                    window=window,
                )
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must not break flow
            logger.warning("telemetry.metric_failed", metric=metric_name, error=str(exc))

    async def record_log(
        self,
        level: str,
        message: str,
        *,
        component: str | None = None,
        context: dict | None = None,
    ) -> None:
        try:
            await self._logs.record(
                SystemLog(
                    level=level,
                    message=message,
                    component=component or "paper",
                    context=context or {},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("telemetry.log_failed", error=str(exc))

    async def latency(self, stage: str, milliseconds: float) -> None:
        await self.record_metric(f"latency_ms_{stage}", milliseconds)

    async def signal_frequency(self, agent: str, count: int = 1) -> None:
        await self.record_metric(f"signal_frequency_{agent}", count)

    async def api_failure(self, provider: str, error: str) -> None:
        await self.record_metric("api_failure", 1)
        await self.record_log(
            "ERROR",
            f"{provider} api failure: {error}",
            component="paper.telemetry",
            context={"provider": provider},
        )

    async def missing_data(self, symbol: str, field_name: str) -> None:
        await self.record_metric("missing_data", 1)

    async def invalid_data(self, symbol: str, reason: str) -> None:
        await self.record_metric("invalid_data", 1)

    async def reconnection(self, provider: str) -> None:
        await self.record_metric("reconnection", 1)
        await self.record_log(
            "WARN",
            f"{provider} reconnected",
            component="paper.telemetry",
            context={"provider": provider},
        )


@dataclass(frozen=True, slots=True)
class OperationalSummary:
    """Aggregated reliability statistics (required output #8)."""

    api_failures: int = 0
    missing_data: int = 0
    invalid_data: int = 0
    reconnections: int = 0
    latency_avg_ms: dict[str, float] = field(default_factory=dict)
    signal_frequency: dict[str, int] = field(default_factory=dict)
    data_events: int = 0
    data_reliability: float = 1.0
    failure_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_failures": self.api_failures,
            "missing_data": self.missing_data,
            "invalid_data": self.invalid_data,
            "reconnections": self.reconnections,
            "latency_avg_ms": dict(self.latency_avg_ms),
            "signal_frequency": dict(self.signal_frequency),
            "data_events": self.data_events,
            "data_reliability": self.data_reliability,
            "failure_rate": self.failure_rate,
        }


def operational_summary(metrics: list[AgentMetric]) -> OperationalSummary:
    """Aggregate a flat list of telemetry metrics into a summary.

    ``data_reliability`` is the share of data events that were *not* missing /
    invalid; ``failure_rate`` is the share of api calls that failed.
    """
    latencies: dict[str, list[float]] = {}
    signal_counts: dict[str, int] = {}
    api_failures = 0.0
    missing = 0.0
    invalid = 0.0
    reconnections = 0.0
    successful_calls = 0.0
    for metric in metrics:
        value = float(metric.value)
        name = metric.metric_name
        if name == "api_failure":
            api_failures += value
        elif name.startswith("signal_frequency_"):
            agent = name.removeprefix("signal_frequency_")
            signal_counts[agent] = signal_counts.get(agent, 0) + int(value)
        elif name.startswith("latency_ms_"):
            stage = name.removeprefix("latency_ms_")
            latencies.setdefault(stage, []).append(value)
        elif name == "missing_data":
            missing += value
        elif name == "invalid_data":
            invalid += value
        elif name == "reconnection":
            reconnections += value
        elif name == "api_call":
            successful_calls += value

    data_events = missing + invalid + sum(signal_counts.values())
    reliability = 1.0
    if data_events:
        reliability = 1.0 - ((missing + invalid) / data_events)
    attempts = successful_calls + api_failures
    failure_rate = api_failures / attempts if attempts else 0.0

    return OperationalSummary(
        api_failures=int(api_failures),
        missing_data=int(missing),
        invalid_data=int(invalid),
        reconnections=int(reconnections),
        latency_avg_ms={
            stage: round(sum(values) / len(values), 3)
            for stage, values in latencies.items()
        },
        signal_frequency=signal_counts,
        data_events=int(data_events),
        data_reliability=round(reliability, 4),
        failure_rate=round(failure_rate, 4),
    )


__all__ = [
    "NullTelemetry",
    "OperationalSummary",
    "OperationalTelemetry",
    "TelemetryRecorder",
    "operational_summary",
]
