"""Unit tests for Phase 7 operational telemetry."""

from __future__ import annotations

from decimal import Decimal

from qtrader.application.paper.telemetry import (
    NullTelemetry,
    OperationalTelemetry,
    operational_summary,
)
from qtrader.domain.entities import AgentMetric
from tests.unit.fakes_paper import FakeAgentMetricRepository, FakeSystemLogRepository


async def test_operational_telemetry_records_metrics_and_logs() -> None:
    metrics = FakeAgentMetricRepository()
    logs = FakeSystemLogRepository()
    telemetry = OperationalTelemetry(agent_metrics=metrics, logs=logs)

    await telemetry.latency("submit", 12.5)
    await telemetry.api_failure("alpaca", "connection refused")
    await telemetry.signal_frequency("technical", 3)
    await telemetry.missing_data("AAPL", "close")
    await telemetry.reconnection("yahoo")

    assert any(
        m.metric_name == "latency_ms_submit" and m.value == Decimal("12.5")
        for m in metrics.metrics
    )
    assert sum(m.value for m in metrics.metrics if m.metric_name == "api_failure") == Decimal("1")
    assert any(
        m.metric_name == "signal_frequency_technical" and m.value == Decimal("3")
        for m in metrics.metrics
    )
    assert any(e.level == "ERROR" and "alpaca" in e.message for e in logs.logs)
    assert any(e.level == "WARN" and "yahoo" in e.message for e in logs.logs)


async def test_operational_telemetry_never_raises(tmp_path) -> None:
    class Boom(FakeAgentMetricRepository):
        async def record(self, metric: AgentMetric) -> AgentMetric:
            raise RuntimeError("db down")

    telemetry = OperationalTelemetry(
        agent_metrics=Boom(), logs=FakeSystemLogRepository()
    )
    await telemetry.latency("submit", 1.0)
    await telemetry.api_failure("broker", "boom")
    await telemetry.record_log("INFO", "still alive")


async def test_null_telemetry_is_a_noop() -> None:
    telemetry = NullTelemetry()
    await telemetry.latency("submit", 1.0)
    await telemetry.api_failure("broker", "x")
    await telemetry.missing_data("AAPL", "close")
    await telemetry.record_log("INFO", "x")


def test_operational_summary_aggregates_metrics() -> None:
    metrics = [
        AgentMetric("paper", "api_failure", Decimal("2")),
        AgentMetric("paper", "api_call", Decimal("10")),
        AgentMetric("paper", "missing_data", Decimal("1")),
        AgentMetric("paper", "invalid_data", Decimal("1")),
        AgentMetric("paper", "reconnection", Decimal("1")),
        AgentMetric("paper", "latency_ms_submit", Decimal("10")),
        AgentMetric("paper", "latency_ms_submit", Decimal("20")),
        AgentMetric("paper", "signal_frequency_news", Decimal("4")),
    ]
    summary = operational_summary(metrics)
    assert summary.api_failures == 2
    assert summary.missing_data == 1
    assert summary.invalid_data == 1
    assert summary.reconnections == 1
    assert summary.latency_avg_ms["submit"] == 15.0
    assert summary.signal_frequency["news"] == 4
    assert summary.data_reliability == 0.6667
    assert summary.failure_rate == 0.1667


def test_operational_summary_empty_is_healthy() -> None:
    summary = operational_summary([])
    assert summary.data_reliability == 1.0
    assert summary.failure_rate == 0.0
    assert summary.api_failures == 0
