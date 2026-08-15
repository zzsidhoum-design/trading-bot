"""Unit tests for Phase 7 paper settings defaults + builder."""

from __future__ import annotations

from qtrader.application.paper.acceptance import AcceptanceThresholds
from qtrader.config.settings import Settings


def test_paper_settings_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.paper_shadow_mode is False
    assert settings.paper_ledger_path == "data/paper/orders.jsonl"
    assert settings.paper_telemetry_enabled is True
    assert settings.paper_accept_min_fill_rate == 0.90
    assert settings.paper_accept_max_slippage_bps == 50.0
    assert settings.paper_accept_max_avg_latency_ms == 5000.0


def test_paper_acceptance_thresholds_builder() -> None:
    settings = Settings(
        _env_file=None,
        paper_accept_min_fill_rate=0.95,
        paper_accept_max_slippage_bps=25.0,
    )
    thresholds = settings.paper_acceptance_thresholds
    assert isinstance(thresholds, AcceptanceThresholds)
    assert thresholds.min_fill_rate == 0.95
    assert thresholds.max_slippage_bps == 25.0
    assert thresholds.max_avg_latency_ms == 5000.0
