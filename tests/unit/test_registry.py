"""Unit tests for the agent registry."""

from __future__ import annotations

from qtrader.application.agents.data import DataAgent
from qtrader.application.agents.registry import default_registry
from qtrader.application.agents.scanner import MarketScanner


def test_default_registry_contains_phase2_agents() -> None:
    registry = default_registry()
    assert "data" in registry.names
    assert "scanner" in registry.names
    assert registry.get("data") is DataAgent
    assert registry.get("scanner") is MarketScanner
    assert registry.get("missing") is None
